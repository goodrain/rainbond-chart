"""Verify the artifact users install, including its embedded migration helper."""

import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[1]


class ChartPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="rainbond-chart-package-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        subprocess.run(["helm", "package", str(CHART), "--destination", str(cls.root)],
                       check=True, capture_output=True, text=True)
        cls.package = next(cls.root.glob("rainbond-*.tgz"))

    def test_archive_contains_exact_script_but_no_tests_bytecode_or_git_data(self):
        with tarfile.open(self.package) as archive:
            names = archive.getnames()
            self.assertFalse(any("/tests/" in name or "__pycache__" in name or name.endswith(".pyc")
                                 or "/.git/" in name or "/.github/" in name for name in names))
            helper = archive.extractfile("rainbond/files/migrate_console_source_mounts.py").read()
            self.assertEqual((CHART / "files/migrate_console_source_mounts.py").read_bytes(), helper)
            self.assertIn("rainbond/docs/console-source-mount-upgrade.md", names)

    def test_install_and_upgrade_render_from_the_actual_archive(self):
        for upgrade in (False, True):
            with self.subTest(upgrade=upgrade):
                command = ["helm", "template", "package-test", str(self.package), "--namespace", "rbd-system",
                           "--kubeconfig", str(self.root / "no-cluster"), "--set", "Cluster.installVersion=v6.11.0-dev"]
                if upgrade:
                    command.append("--is-upgrade")
                result = subprocess.run(command, check=True, capture_output=True, text=True,
                                        env=dict(os.environ, KUBECONFIG=str(self.root / "no-cluster")))
                objects = [obj for obj in yaml.safe_load_all(result.stdout) if obj]
                hooks = [obj for obj in objects if obj.get("metadata", {}).get("annotations", {}).get("helm.sh/hook")]
                self.assertEqual(6 if upgrade else 0, len(hooks))
                jobs = [obj for obj in hooks if obj["kind"] == "Job"]
                self.assertEqual(2 if upgrade else 0, len(jobs))
                components = [item for obj in objects if obj.get("kind") == "List" for item in obj.get("items", [])]
                console = next(obj for obj in components if obj["metadata"]["name"] == "rbd-app-ui")
                self.assertTrue(console["spec"]["image"].endswith(":v6.11.0-dev"))
                self.assertEqual("Always", console["spec"]["imagePullPolicy"])
                self.assertNotIn("volumeMounts", console["spec"])
                self.assertNotIn("volumes", console["spec"])
                for hook in hooks:
                    if hook["kind"] == "ConfigMap":
                        self.assertEqual((CHART / "files/migrate_console_source_mounts.py").read_text(),
                                         hook["data"]["migrate_console_source_mounts.py"])


if __name__ == "__main__":
    unittest.main()
