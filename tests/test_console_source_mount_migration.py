"""Behavioral regressions for retiring recognized Console source overrides.

Fixtures contain small synthetic source files, not environment data. Only the
approved fingerprint catalog is substituted; hashing, planning, CAS patches,
prepare/apply ordering, and compensation use the production implementation.
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY / "files" / "migrate_console_source_mounts.py"
SPEC = importlib.util.spec_from_file_location("console_source_mount_migration", MODULE_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

SOURCE_PATHS = (
    "console/services/app_actions/app_manage.py",
    "console/views/app_overview.py",
    "console/urls/__init__.py",
    "console/utils/vm_vnc_proxy.py",
    "console/views/vm_vnc.py",
    "console/utils/vm_vnc_proxy_relay.py",
)
BASELINE_IMAGE = "registry.example/rainbond@sha256:" + "1" * 64
TARGET_DIGEST = "registry.example/rainbond@sha256:" + "2" * 64
OTHER_DIGEST = "registry.example/rainbond@sha256:" + "3" * 64
TARGET_IMAGE = "registry.example/rainbond:v6.11.0-dev"
SOURCE_SENTINEL = "__UNRECOGNIZED_SOURCE_MUST_NOT_APPEAR_IN_ERRORS__"


def fingerprint(source):
    return hashlib.sha256(source.encode()).hexdigest()


def ready_pod(digest, image=TARGET_IMAGE):
    return {
        "metadata": {"name": "console-pod", "labels": {"name": "rbd-app-ui"}},
        "spec": {"containers": [{"name": "rbd-app-ui", "image": image}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{
                "name": "rbd-app-ui", "ready": True,
                "imageID": "docker-pullable://" + digest,
            }],
        },
    }


class FakeAPI:
    """A tiny resource store with JSON Patch test/CAS and isolated snapshots."""

    def __init__(self, component, configmaps):
        self.component = copy.deepcopy(component)
        self.configmaps = copy.deepcopy(configmaps)
        self.backups = {}
        self.calls = []
        self.patches = []
        self.successful_patches = 0
        self.pods = [ready_pod(BASELINE_IMAGE, "registry.example/rainbond:previous")]
        self.own_image_id = TARGET_DIGEST
        self.fail_patch = False

    def get_component(self):
        self.calls.append(("get_component",))
        return copy.deepcopy(self.component)

    def get_configmap(self, name):
        self.calls.append(("get_configmap", name))
        if name not in self.configmaps:
            raise migration.MigrationError("configuration source is unavailable")
        return copy.deepcopy(self.configmaps[name])

    def create_backup(self, name, payload):
        self.calls.append(("create_backup", name))
        if name in self.backups:
            raise migration.MigrationError("backup already exists")
        self.backups[name] = copy.deepcopy(payload)

    def get_backup(self, name):
        self.calls.append(("get_backup", name))
        return copy.deepcopy(self.backups.get(name))

    def get_pods(self):
        self.calls.append(("get_pods",))
        return copy.deepcopy(self.pods)

    def get_own_image_id(self):
        self.calls.append(("get_own_image_id",))
        return self.own_image_id

    @staticmethod
    def path_parent(document, path):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
        parent = document
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        return parent, parts[-1]

    def patch_component(self, patch):
        self.calls.append(("patch_component",))
        self.patches.append(copy.deepcopy(patch))
        if self.fail_patch:
            raise migration.MigrationError("resource version conflict")
        updated = copy.deepcopy(self.component)
        for operation in patch:
            parent, key = self.path_parent(updated, operation["path"])
            if isinstance(parent, list):
                key = int(key)
            if operation["op"] == "test":
                if parent[key] != operation["value"]:
                    raise migration.MigrationError("resource version or source configuration changed")
            elif operation["op"] in ("replace", "add"):
                parent[key] = copy.deepcopy(operation["value"])
            elif operation["op"] == "remove":
                del parent[key]
            else:
                raise AssertionError("unexpected patch operation")
        updated["metadata"]["resourceVersion"] = str(int(updated["metadata"]["resourceVersion"]) + 1)
        self.component = updated
        self.successful_patches += 1
        return copy.deepcopy(updated)

    def chart_updated_image(self):
        self.component["spec"]["image"] = TARGET_IMAGE
        self.component["metadata"]["resourceVersion"] = "12"
        self.pods = [ready_pod(TARGET_DIGEST)]


class ProductionCatalogTest(unittest.TestCase):
    def test_catalog_covers_only_the_six_reviewed_console_files(self):
        self.assertEqual(set(SOURCE_PATHS), set(migration.NATIVE_BUNDLE))
        self.assertEqual(set(SOURCE_PATHS), set(migration.LEGACY_HASHES))
        for value in migration.NATIVE_BUNDLE.values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")


class SourceMountMigrationTest(unittest.TestCase):
    def setUp(self):
        self.native_sources = {relative: "# native fixture: " + relative + "\n" for relative in SOURCE_PATHS}
        self.old_sources = {relative: "# reviewed legacy fixture: " + relative + "\n" for relative in SOURCE_PATHS}
        self.native_hashes = {relative: fingerprint(value) for relative, value in self.native_sources.items()}
        legacy_hashes = {relative: {fingerprint(value), self.native_hashes[relative]}
                         for relative, value in self.old_sources.items()}
        self.catalog_patches = [
            mock.patch.object(migration, "NATIVE_BUNDLE", self.native_hashes),
            mock.patch.object(migration, "LEGACY_HASHES", legacy_hashes),
        ]
        for patch in self.catalog_patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.component = {
            "apiVersion": "rainbond.io/v1alpha1", "kind": "RbdComponent",
            "metadata": {"name": "rbd-app-ui", "namespace": "qa-rainbond", "uid": "component-uid", "resourceVersion": "10"},
            "spec": {
                "image": "registry.example/rainbond:previous", "replicas": 1,
                "unrelatedFutureField": {"must": "survive"},
                "volumeMounts": [], "volumes": [],
            },
        }
        self.configmaps = {}
        for name in ("arbitrary-reviewed-source", "arbitrary-reviewed-relay"):
            self.configmaps[name] = {
                "metadata": {"name": name, "namespace": "qa-rainbond", "uid": name + "-uid", "resourceVersion": "7"},
                "immutable": True, "data": {},
            }
            self.component["spec"]["volumes"].append({"name": name, "configMap": {"name": name}})
        for index, relative in enumerate(SOURCE_PATHS):
            name = "arbitrary-reviewed-relay" if relative.endswith("relay.py") else "arbitrary-reviewed-source"
            key = "source-" + str(index)
            self.configmaps[name]["data"][key] = self.old_sources[relative]
            self.component["spec"]["volumeMounts"].append({
                "name": name, "mountPath": "/app/ui/" + relative,
                "readOnly": True, "subPath": key,
            })
        self.client = FakeAPI(self.component, self.configmaps)
        self.baseline = {"image": BASELINE_IMAGE, "files": copy.deepcopy(self.native_hashes)}

    def plan(self, component=None):
        return migration.plan_migration(
            self.client.component if component is None else component,
            self.client.get_configmap, self.native_hashes)

    def prepare(self, **overrides):
        arguments = {
            "client": self.client, "target_image": TARGET_IMAGE,
            "backup_name": "source-mount-backup-r2", "revision": "2",
            "baseline_proof": self.baseline, "native_hashes": self.native_hashes,
        }
        arguments.update(overrides)
        return migration.prepare(**arguments)

    def apply(self, **overrides):
        arguments = {
            "client": self.client, "target_image": TARGET_IMAGE,
            "backup_name": "source-mount-backup-r2", "revision": "2",
            "native_hashes": self.native_hashes, "waiter": lambda *args: None,
        }
        arguments.update(overrides)
        return migration.apply(**arguments)

    def prepared_target(self):
        result = self.prepare()
        self.assertEqual("prepared", result["status"])
        self.assertEqual([], self.client.patches, "preparation must not change live CR source mounts")
        self.client.chart_updated_image()

    def assert_no_mutation(self, operation):
        original = copy.deepcopy(self.client.component)
        with self.assertRaises(migration.MigrationError):
            operation()
        self.assertEqual(original, self.client.component)
        self.assertEqual(0, self.client.successful_patches)

    def test_new_install_without_component_skips_native_and_backup_reads(self):
        self.client.component = None
        result = self.prepare(native_hashes={}, baseline_proof={})
        self.assertEqual("noop", result["status"])
        self.assertFalse(any(call[0] in ("get_configmap", "get_backup", "create_backup", "get_pods", "get_own_image_id")
                             for call in self.client.calls))

    def test_unmodified_install_without_source_mounts_is_noop(self):
        self.client.component["spec"].pop("volumeMounts")
        self.client.component["spec"].pop("volumes")
        self.assertIsNone(self.plan())
        result = self.prepare(native_hashes={}, baseline_proof={})
        self.assertEqual("noop", result["status"])
        self.assertEqual([], self.client.patches)
        self.assertEqual({}, self.client.backups)

    def test_unrelated_custom_python_and_standard_migrations_are_left_alone(self):
        self.client.component["spec"]["volumeMounts"] = [
            {"name": "custom", "mountPath": "/app/ui/console/extensions/private.py", "subPath": "private.py"},
            {"name": "data", "mountPath": "/app/ui/console/migrations", "subPath": "console/migrations"},
            {"name": "data", "mountPath": "/app/ui/www/migrations", "subPath": "www/migrations"},
        ]
        self.assertIsNone(self.plan())
        self.assertEqual("noop", self.prepare(native_hashes={}, baseline_proof={})["status"])
        self.assertEqual({}, self.client.backups)
        self.assertEqual([], self.client.patches)

    def test_complete_known_bundle_retires_six_mounts_and_only_unused_source_volumes(self):
        original = copy.deepcopy(self.client.component)
        plan = self.plan()
        self.assertEqual([], plan["mounts"])
        self.assertEqual([], plan["volumes"])
        self.assertEqual({"/app/ui/" + path for path in SOURCE_PATHS}, set(plan["removed_paths"]))
        self.assertEqual(original, self.client.component)
        self.assertEqual([], self.client.patches)

    def test_subset_of_reviewed_mounts_can_retire_without_requiring_the_full_old_bundle(self):
        self.client.component["spec"]["volumeMounts"] = self.client.component["spec"]["volumeMounts"][:2]
        plan = self.plan()
        self.assertEqual([], plan["mounts"])
        self.assertEqual(2, len(plan["removed_paths"]))

    def test_known_path_already_identical_to_native_is_still_redundant(self):
        mount = self.client.component["spec"]["volumeMounts"][0]
        self.client.configmaps[mount["name"]]["data"][mount["subPath"]] = self.native_sources[SOURCE_PATHS[0]]
        self.assertEqual(6, len(self.plan()["removed_paths"]))

    def test_unrecognized_custom_config_and_python_mounts_and_shared_volume_are_preserved(self):
        unknown = [
            {"name": "arbitrary-reviewed-source", "mountPath": "/etc/console/custom.conf", "subPath": "extra-config", "readOnly": True},
            {"name": "private-extension", "mountPath": "/app/ui/console/extensions/private.py", "subPath": "private.py", "readOnly": True},
        ]
        private_volume = {"name": "private-extension", "secret": {"secretName": "not-read-by-migration"}}
        self.client.component["spec"]["volumeMounts"].extend(copy.deepcopy(unknown))
        self.client.component["spec"]["volumes"].append(copy.deepcopy(private_volume))
        self.client.configmaps["arbitrary-reviewed-source"]["data"]["extra-config"] = "# custom configuration\n"
        plan = self.plan()
        self.assertEqual(unknown, plan["mounts"])
        self.assertEqual([self.component["spec"]["volumes"][0], private_volume], plan["volumes"])
        self.assertTrue(all(call[1] != "private-extension" for call in self.client.calls if call[0] == "get_configmap"))

    def test_unknown_source_hash_for_known_path_fails_without_exposing_source(self):
        mount = self.client.component["spec"]["volumeMounts"][0]
        self.client.configmaps[mount["name"]]["data"][mount["subPath"]] = SOURCE_SENTINEL
        with self.assertRaises(migration.MigrationError) as caught:
            self.plan()
        self.assertNotIn(SOURCE_SENTINEL, str(caught.exception))
        self.assertEqual([], self.client.patches)

    def test_core_source_directory_mount_requires_review(self):
        self.client.component["spec"]["volumeMounts"].append({
            "name": "arbitrary-reviewed-source", "mountPath": "/app/ui/console", "readOnly": True})
        self.assert_no_mutation(self.prepare)

    def test_enclosing_source_directory_alone_cannot_bypass_review(self):
        self.client.component["spec"]["volumeMounts"] = [{
            "name": "arbitrary-reviewed-source", "mountPath": "/app/ui/console", "readOnly": True}]
        self.assert_no_mutation(self.prepare)

    def test_known_source_requires_readonly_file_subpath_configmap(self):
        for change in ({"readOnly": False}, {"subPath": ""}, {"subPathExpr": "$(SOURCE_FILE)"}):
            with self.subTest(change=change):
                self.client.component = copy.deepcopy(self.component)
                self.client.component["spec"]["volumeMounts"][0].update(change)
                self.assert_no_mutation(self.prepare)

    def test_known_source_from_secret_volume_is_never_read_or_retired(self):
        volume = self.client.component["spec"]["volumes"][0]
        volume.pop("configMap")
        volume["secret"] = {"secretName": "private-source-not-read"}
        self.assert_no_mutation(self.prepare)
        self.assertFalse(any(call[0] == "get_configmap" for call in self.client.calls))

    def test_configmap_items_mapping_resolves_the_exact_file_key(self):
        volume = self.client.component["spec"]["volumes"][0]
        original_keys = list(self.client.configmaps[volume["name"]]["data"])
        volume["configMap"]["items"] = [
            {"key": key, "path": "projected/" + key} for key in original_keys]
        for mount in self.client.component["spec"]["volumeMounts"]:
            if mount["name"] == volume["name"]:
                mount["subPath"] = "projected/" + mount["subPath"]
        self.assertEqual(6, len(self.plan()["removed_paths"]))

    def test_ambiguous_configmap_items_mapping_is_rejected(self):
        volume = self.client.component["spec"]["volumes"][0]
        mount = self.client.component["spec"]["volumeMounts"][0]
        volume["configMap"]["items"] = [
            {"key": mount["subPath"], "path": mount["subPath"]},
            {"key": "another-key", "path": mount["subPath"]},
        ]
        self.assert_no_mutation(self.prepare)

    def test_missing_or_mutable_configmap_rejects_migration(self):
        for action in ("missing", "mutable"):
            with self.subTest(action=action):
                self.client.configmaps = copy.deepcopy(self.configmaps)
                if action == "missing":
                    del self.client.configmaps["arbitrary-reviewed-source"]
                else:
                    self.client.configmaps["arbitrary-reviewed-source"]["immutable"] = False
                self.assert_no_mutation(self.prepare)

    def test_missing_or_unapproved_native_file_rejects_before_backup_or_patch(self):
        for bad in ({}, {**self.native_hashes, SOURCE_PATHS[0]: None}, {**self.native_hashes, SOURCE_PATHS[0]: "f" * 64}):
            with self.subTest(native=bad):
                self.assert_no_mutation(lambda: self.prepare(native_hashes=bad))
                self.assertEqual({}, self.client.backups)

    def test_native_file_collection_hashes_real_bytes_and_marks_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, source in list(self.native_sources.items())[:-1]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            actual = migration.native_files(root=root)
        for relative in SOURCE_PATHS[:-1]:
            self.assertEqual(self.native_hashes[relative], actual[relative])
        self.assertIsNone(actual[SOURCE_PATHS[-1]])

    def test_patch_has_uid_resourceversion_image_and_complete_array_preconditions(self):
        original = copy.deepcopy(self.client.component)
        plan = self.plan()
        patch = migration.build_patch(self.client.component, plan)
        tests = {operation["path"]: operation["value"] for operation in patch if operation["op"] == "test"}
        self.assertEqual({
            "/metadata/uid": original["metadata"]["uid"],
            "/metadata/resourceVersion": original["metadata"]["resourceVersion"],
            "/spec/image": original["spec"]["image"],
            "/spec/volumeMounts": original["spec"]["volumeMounts"],
            "/spec/volumes": original["spec"]["volumes"],
        }, tests)
        self.assertEqual({"/spec/volumeMounts", "/spec/volumes"},
                         {operation["path"] for operation in patch if operation["op"] != "test"})
        self.client.patch_component(patch)
        self.assertEqual(original["spec"]["image"], self.client.component["spec"]["image"])
        self.assertEqual(original["spec"]["unrelatedFutureField"], self.client.component["spec"]["unrelatedFutureField"])

    def test_ready_pod_is_not_migration_success_when_sources_reappear_in_the_cr(self):
        plan = self.plan()
        self.client.chart_updated_image()
        self.client.component["spec"]["volumes"] = plan["volumes"]
        # A concurrent writer restores source mounts before its next Pod rollout.
        # The still-clean Ready Pod must not make this desired state look migrated.
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 100]), \
                mock.patch.object(migration.time, "sleep"):
            with self.assertRaises(migration.MigrationError):
                migration.wait_for_rollout(self.client, "sha256:" + "2" * 64, plan)

    def test_same_mutable_tag_uses_verified_baseline_to_trigger_the_source_rollout(self):
        self.client.component["spec"].update(image=TARGET_IMAGE, imagePullPolicy="Always")
        self.client.pods = [ready_pod(BASELINE_IMAGE, TARGET_IMAGE)]
        self.prepare()
        patch = self.client.patch_component

        def rollout_on_patch(operations):
            result = patch(operations)
            self.client.pods = [ready_pod(TARGET_DIGEST)]
            return result

        self.client.patch_component = rollout_on_patch
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 61, 61]):
            self.assertEqual("migrated", self.apply(waiter=None)["status"])
        self.assertEqual(1, self.client.successful_patches)
        self.assertEqual({"sha256:" + "2" * 64}, migration.ready_image_digests(self.client.pods))

    def test_same_mutable_tag_without_always_pull_cannot_retire_sources_using_the_old_pod(self):
        self.client.component["spec"].update(image=TARGET_IMAGE, imagePullPolicy="IfNotPresent")
        self.client.pods = [ready_pod(BASELINE_IMAGE, TARGET_IMAGE)]
        self.prepare()
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 61]), mock.patch.object(migration.time, "sleep"):
            self.assert_no_mutation(self.apply)

    def test_same_tag_pull_policy_change_before_patch_is_not_overwritten(self):
        self.client.component["spec"].update(image=TARGET_IMAGE, imagePullPolicy="Always")
        self.client.pods = [ready_pod(BASELINE_IMAGE, TARGET_IMAGE)]
        self.prepare()

        def concurrent_policy_change(_):
            self.client.component["spec"]["imagePullPolicy"] = "IfNotPresent"
            self.client.component["metadata"]["resourceVersion"] = "11"

        self.client.ensure_time_budget = concurrent_policy_change
        with self.assertRaises(migration.MigrationError):
            self.apply()
        self.assertEqual(0, self.client.successful_patches)
        self.assertEqual("IfNotPresent", self.client.component["spec"]["imagePullPolicy"])

    def test_same_tag_rollout_staying_on_baseline_digest_restores_previous_sources(self):
        self.client.component["spec"].update(image=TARGET_IMAGE, imagePullPolicy="Always")
        self.client.pods = [ready_pod(BASELINE_IMAGE, TARGET_IMAGE)]
        self.prepare()
        before = copy.deepcopy(self.client.component["spec"])
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 0, 100]), \
                mock.patch.object(migration.time, "sleep"):
            with self.assertRaises(migration.MigrationError):
                self.apply(waiter=None)
        self.assertEqual(2, self.client.successful_patches)
        self.assertEqual(before, self.client.component["spec"])

    def test_lost_patch_response_restores_mounts_when_the_api_already_applied_the_patch(self):
        self.prepared_target()
        before = copy.deepcopy(self.client.component)
        patch = self.client.patch_component
        calls = []

        def response_lost_once(operations):
            result = patch(operations)
            calls.append(True)
            if len(calls) == 1:
                raise OSError("response lost fixture")
            return result

        self.client.patch_component = response_lost_once
        with self.assertRaises(migration.MigrationError) as caught:
            self.apply()
        self.assertIn("restored", str(caught.exception))
        self.assertEqual(before["spec"], self.client.component["spec"])
        self.assertEqual(2, self.client.successful_patches)

    def test_rejected_initial_patch_leaves_sources_and_does_not_attempt_a_second_patch(self):
        self.prepared_target()
        before = copy.deepcopy(self.client.component)
        self.client.fail_patch = True
        with self.assertRaises(migration.MigrationError):
            self.apply()
        self.assertEqual(before, self.client.component)
        self.assertEqual(1, len(self.client.patches))
        self.assertEqual(0, self.client.successful_patches)

    def test_insufficient_job_budget_rejects_before_changing_source_mounts(self):
        self.prepared_target()
        self.client.ensure_time_budget = mock.Mock(side_effect=migration.MigrationError("budget fixture"))
        self.assert_no_mutation(self.apply)
        self.client.ensure_time_budget.assert_called_once_with(150)

    def test_admission_parent_mount_cannot_hide_the_image_before_migration(self):
        self.prepared_target()
        self.client.pods[0]["spec"]["containers"][0]["volumeMounts"] = [
            {"name": "unreviewed-source", "mountPath": "/app/ui/console", "readOnly": True}]
        self.assert_no_mutation(self.apply)

    def test_admission_replaced_source_volume_is_rejected_even_when_mount_path_matches(self):
        self.prepared_target()
        mount = copy.deepcopy(self.client.component["spec"]["volumeMounts"][0])
        self.client.pods[0]["spec"]["containers"][0]["volumeMounts"] = [mount]
        self.client.pods[0]["spec"]["volumes"] = [{"name": mount["name"], "configMap": {"name": "unreviewed-map"}}]
        self.assert_no_mutation(self.apply)

    def test_prepare_records_backup_without_changing_live_source_mounts(self):
        original = copy.deepcopy(self.client.component)
        self.assertEqual("prepared", self.prepare()["status"])
        self.assertEqual(original, self.client.component)
        self.assertEqual([], self.client.patches)
        self.assertIn("source-mount-backup-r2", self.client.backups)
        backup_text = json.dumps(self.client.backups)
        for source in self.old_sources.values():
            self.assertNotIn(source.strip(), backup_text)

    def test_backup_excludes_unknown_private_volume_data_while_migration_and_restore_preserve_it(self):
        private_value = "private-configuration-fixture"
        private_mount = {"name": "private-flex", "mountPath": "/custom/private-config", "readOnly": True}
        private_volume = {
            "name": "private-flex",
            "flexVolume": {"driver": "example.invalid/private-driver", "options": {"literal": private_value}},
        }
        for outcome in ("success", "rollback"):
            with self.subTest(outcome=outcome):
                component = copy.deepcopy(self.component)
                component["spec"]["volumeMounts"].append(copy.deepcopy(private_mount))
                component["spec"]["volumes"].append(copy.deepcopy(private_volume))
                self.client = FakeAPI(component, self.configmaps)
                self.assertEqual("prepared", self.prepare()["status"])
                self.assertNotIn(private_value, json.dumps(self.client.backups),
                                 "ordinary ConfigMap backups must not duplicate unreviewed private volume configuration")
                self.assertEqual(component, self.client.component)
                self.client.chart_updated_image()
                before = copy.deepcopy(self.client.component)

                if outcome == "rollback":
                    def failed_waiter(*_):
                        raise migration.MigrationError("rollout failed in privacy fixture")

                    with self.assertRaises(migration.MigrationError):
                        self.apply(waiter=failed_waiter)
                    self.assertEqual(before["spec"], self.client.component["spec"])
                    self.assertEqual(2, self.client.successful_patches)
                else:
                    self.assertEqual("migrated", self.apply()["status"])
                    self.assertEqual([private_mount], self.client.component["spec"]["volumeMounts"])
                    self.assertEqual([private_volume], self.client.component["spec"]["volumes"])
                self.assertIn(private_mount, self.client.component["spec"]["volumeMounts"])
                self.assertIn(private_volume, self.client.component["spec"]["volumes"])

    def test_prepare_requires_rollback_baseline_native_files_and_current_digest(self):
        cases = [
            {"image": BASELINE_IMAGE, "files": {}},
            {"image": BASELINE_IMAGE, "files": {**self.native_hashes, SOURCE_PATHS[3]: None}},
            {"image": OTHER_DIGEST, "files": self.native_hashes},
        ]
        for proof in cases:
            with self.subTest(proof=proof):
                self.assert_no_mutation(lambda: self.prepare(baseline_proof=proof))
                self.assertEqual({}, self.client.backups)

    def test_prepare_rejects_mixed_ready_baseline_digests(self):
        self.client.pods.append(ready_pod(OTHER_DIGEST))
        self.assert_no_mutation(self.prepare)

    def test_prepare_requires_running_pod_image_to_match_current_cr(self):
        self.client.pods[0]["spec"]["containers"][0]["image"] = "registry.example/rainbond:another-baseline"
        self.assert_no_mutation(self.prepare)

    def test_prepare_requires_an_actual_ready_baseline_pod_and_valid_digest(self):
        for changed in ("absent", "container-not-ready", "pod-not-ready", "invalid-digest", "own-invalid-digest"):
            with self.subTest(changed=changed):
                self.client = FakeAPI(self.component, self.configmaps)
                if changed == "absent":
                    self.client.pods = []
                elif changed == "container-not-ready":
                    self.client.pods[0]["status"]["containerStatuses"][0]["ready"] = False
                elif changed == "pod-not-ready":
                    self.client.pods[0]["status"]["conditions"][0]["status"] = "False"
                elif changed == "invalid-digest":
                    self.client.pods[0]["status"]["containerStatuses"][0]["imageID"] = "unverified-image-reference"
                else:
                    self.client.own_image_id = "unverified-migration-image-reference"
                self.assert_no_mutation(self.prepare)
                self.assertEqual({}, self.client.backups)

    def test_apply_requires_backup_and_chart_target_image(self):
        self.client.chart_updated_image()
        self.assert_no_mutation(self.apply)
        self.client = FakeAPI(self.component, self.configmaps)
        self.prepare()
        self.assert_no_mutation(self.apply)

    def test_apply_retires_source_overrides_after_chart_image_update_then_is_idempotent(self):
        self.prepared_target()
        original = copy.deepcopy(self.client.component)
        waiter = mock.Mock()
        result = self.apply(waiter=waiter)
        self.assertEqual("migrated", result["status"])
        self.assertEqual([], self.client.component["spec"]["volumeMounts"])
        self.assertEqual([], self.client.component["spec"]["volumes"])
        self.assertEqual(TARGET_IMAGE, self.client.component["spec"]["image"])
        self.assertEqual(original["spec"]["unrelatedFutureField"], self.client.component["spec"]["unrelatedFutureField"])
        self.assertEqual(1, self.client.successful_patches)
        waiter.assert_called_once()
        repeat = self.apply()
        self.assertIn(repeat["status"], ("noop", "already-migrated"))
        self.assertEqual(1, self.client.successful_patches)

    def test_apply_rejects_changed_revision_or_recreated_component(self):
        self.prepared_target()
        self.assert_no_mutation(lambda: self.apply(revision="3"))
        self.client.component["metadata"]["uid"] = "replacement-component"
        self.assert_no_mutation(self.apply)

    def test_apply_rejects_changed_source_uid_even_with_identical_content(self):
        self.prepared_target()
        self.client.configmaps["arbitrary-reviewed-source"]["metadata"]["uid"] = "replacement-map"
        self.assert_no_mutation(self.apply)

    def test_apply_rejects_changed_source_data_and_mappings(self):
        self.prepared_target()
        mount = self.client.component["spec"]["volumeMounts"][0]
        self.client.configmaps[mount["name"]]["data"][mount["subPath"]] = SOURCE_SENTINEL
        self.assert_no_mutation(self.apply)
        self.client.configmaps = copy.deepcopy(self.configmaps)
        self.client.component["spec"]["volumeMounts"][0]["subPath"] = "a-different-key"
        self.assert_no_mutation(self.apply)

    def test_apply_requires_same_target_digest_in_job_backup_and_ready_console(self):
        self.prepared_target()
        self.client.own_image_id = OTHER_DIGEST
        self.assert_no_mutation(self.apply)
        self.client.own_image_id = TARGET_DIGEST
        self.client.pods = [ready_pod(OTHER_DIGEST)]
        self.assert_no_mutation(self.apply)

    def test_apply_requires_ready_pod_image_to_match_chart_target(self):
        self.prepared_target()
        self.client.pods[0]["spec"]["containers"][0]["image"] = "registry.example/rainbond:another-target"
        self.assert_no_mutation(self.apply)

    def test_disconnected_job_retry_waits_for_already_migrated_revision(self):
        self.prepared_target()
        self.client.patch_component(migration.build_patch(self.client.component, self.plan()))
        waiter = mock.Mock()
        result = self.apply(waiter=waiter)
        self.assertEqual("already-migrated", result["status"])
        waiter.assert_called_once()
        self.assertEqual(1, self.client.successful_patches)

    def test_apply_waits_for_console_to_switch_from_baseline_to_verified_target(self):
        self.prepared_target()
        self.client.pods = [ready_pod(BASELINE_IMAGE, self.component["spec"]["image"])]

        def advance_rollout(_):
            self.client.pods = [ready_pod(TARGET_DIGEST)]

        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 1]), \
                mock.patch.object(migration.time, "sleep", side_effect=advance_rollout) as sleep:
            self.assertEqual("migrated", self.apply()["status"])
        sleep.assert_called_once()
        self.assertEqual(1, self.client.successful_patches)

    def test_waiting_for_target_refreshes_status_resourceversion_and_preserves_new_fields(self):
        self.prepared_target()
        self.client.pods = [ready_pod(BASELINE_IMAGE, self.component["spec"]["image"])]

        def operator_observed_new_image(_):
            self.client.pods = [ready_pod(TARGET_DIGEST)]
            self.client.component["metadata"]["resourceVersion"] = "13"
            self.client.component["status"] = {"observedGeneration": 2}
            self.client.component["spec"]["unrelatedFutureField"] = {"must": "preserve-concurrent-value"}

        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 1]), \
                mock.patch.object(migration.time, "sleep", side_effect=operator_observed_new_image):
            self.assertEqual("migrated", self.apply()["status"])
        self.assertEqual(1, self.client.successful_patches)
        tests = {operation["path"]: operation["value"] for operation in self.client.patches[0] if operation["op"] == "test"}
        self.assertEqual("13", tests["/metadata/resourceVersion"])
        self.assertEqual({"must": "preserve-concurrent-value"}, self.client.component["spec"]["unrelatedFutureField"])
        self.assertEqual({"observedGeneration": 2}, self.client.component["status"])

    def test_waiting_for_target_does_not_patch_concurrent_source_image_or_identity_changes(self):
        for changed in ("mounts", "image", "uid"):
            with self.subTest(changed=changed):
                self.client = FakeAPI(self.component, self.configmaps)
                self.prepared_target()
                self.client.pods = [ready_pod(BASELINE_IMAGE, self.component["spec"]["image"])]
                concurrent = {}

                def changed_while_waiting(_):
                    self.client.pods = [ready_pod(TARGET_DIGEST)]
                    self.client.component["metadata"]["resourceVersion"] = "13"
                    if changed == "mounts":
                        self.client.component["spec"]["volumeMounts"].append({"name": "new-custom", "mountPath": "/custom"})
                        self.client.component["spec"]["volumes"].append({"name": "new-custom", "emptyDir": {}})
                    elif changed == "image":
                        self.client.component["spec"]["image"] = "registry.example/rainbond:another-target"
                    else:
                        self.client.component["metadata"]["uid"] = "recreated-during-target-switch"
                    concurrent.update(copy.deepcopy(self.client.component))

                with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 1]), \
                        mock.patch.object(migration.time, "sleep", side_effect=changed_while_waiting):
                    with self.assertRaises(migration.MigrationError):
                        self.apply()
                self.assertEqual(concurrent, self.client.component)
                self.assertEqual(0, self.client.successful_patches)
                self.assertEqual([], self.client.patches)

    def test_apply_times_out_unready_target_before_removing_any_mounts(self):
        self.prepared_target()
        self.client.pods[0]["status"]["containerStatuses"][0]["ready"] = False
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 100]), \
                mock.patch.object(migration.time, "sleep"):
            self.assert_no_mutation(self.apply)

    def test_rollout_rejects_admission_directory_mount_hiding_verified_native_files(self):
        self.prepared_target()
        plan = self.plan()
        self.client.patch_component(migration.build_patch(self.client.component, plan))
        self.client.pods[0]["spec"]["containers"][0]["volumeMounts"] = [
            {"name": "admission-source", "mountPath": "/app/ui/console"}]
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 100]), \
                mock.patch.object(migration.time, "sleep"):
            with self.assertRaises(migration.MigrationError):
                migration.wait_for_rollout(self.client, "sha256:" + "2" * 64, plan)

    def test_apply_refuses_native_image_files_changed_since_prepare(self):
        self.prepared_target()
        bad_hashes = {**self.native_hashes, SOURCE_PATHS[1]: "f" * 64}
        self.assert_no_mutation(lambda: self.apply(native_hashes=bad_hashes))

    def test_compare_and_swap_rejects_intervening_resourceversion_without_mutation(self):
        patch = migration.build_patch(self.client.component, self.plan())
        self.client.component["metadata"]["resourceVersion"] = "11"
        self.assert_no_mutation(lambda: self.client.patch_component(patch))

    def test_apply_patch_conflict_does_not_force_or_overwrite_source_arrays(self):
        self.prepared_target()
        self.client.fail_patch = True
        self.assert_no_mutation(self.apply)
        self.assertEqual(1, len(self.client.patches))

    def test_rollout_failure_restores_original_mounts_without_changing_target_image(self):
        self.prepared_target()
        original = copy.deepcopy(self.client.component)

        def failed_waiter(*_):
            raise migration.MigrationError("rollout did not become ready")

        with self.assertRaises(migration.MigrationError):
            self.apply(waiter=failed_waiter)
        self.assertEqual(original["spec"], self.client.component["spec"])
        self.assertEqual(2, self.client.successful_patches)

    def test_rollout_failure_never_overwrites_concurrent_image_or_mount_changes(self):
        for changed in ("image", "mounts", "uid"):
            with self.subTest(changed=changed):
                self.client = FakeAPI(self.component, self.configmaps)
                self.prepared_target()

                def failed_waiter(*_):
                    if changed == "image":
                        self.client.component["spec"]["image"] = "registry.example/rainbond:another-update"
                    elif changed == "mounts":
                        self.client.component["spec"]["volumeMounts"].append({"name": "new-custom", "mountPath": "/custom"})
                    else:
                        self.client.component["metadata"]["uid"] = "recreated-during-rollout"
                    raise migration.MigrationError("rollout failed after concurrent change")

                with self.assertRaises(migration.MigrationError):
                    self.apply(waiter=failed_waiter)
                self.assertEqual(1, self.client.successful_patches)
                if changed == "mounts":
                    self.assertEqual([{"name": "new-custom", "mountPath": "/custom"}], self.client.component["spec"]["volumeMounts"])
                else:
                    self.assertEqual([], self.client.component["spec"]["volumeMounts"])

    def test_rollout_failure_cannot_restore_replaced_mutable_or_changed_source_content(self):
        for changed in ("uid", "mutable", "data", "missing"):
            with self.subTest(changed=changed):
                self.client = FakeAPI(self.component, self.configmaps)
                self.prepared_target()

                def failed_waiter(*_):
                    source = self.client.configmaps["arbitrary-reviewed-source"]
                    if changed == "uid":
                        source["metadata"]["uid"] = "recreated-source-map"
                    elif changed == "mutable":
                        source["immutable"] = False
                    elif changed == "data":
                        source["data"]["source-0"] = SOURCE_SENTINEL
                    else:
                        del self.client.configmaps["arbitrary-reviewed-source"]
                    raise migration.MigrationError("rollout failed after source changed")

                with self.assertRaises(migration.MigrationError) as caught:
                    self.apply(waiter=failed_waiter)
                self.assertNotIn(SOURCE_SENTINEL, str(caught.exception))
                self.assertEqual(1, self.client.successful_patches)
                self.assertEqual([], self.client.component["spec"]["volumeMounts"])


if __name__ == "__main__":
    unittest.main()
