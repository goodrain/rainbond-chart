"""Render the real chart; never use the caller's cluster or credentials."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import yaml


CHART = Path(__file__).resolve().parents[1]
COMPONENT = "console-source-mount-migration"
DIGEST = "registry.example.test/rainbond@sha256:" + "a" * 64


@contextlib.contextmanager
def api_server(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            state.setdefault("_requests", []).append(path)
            groups = [{
                "name": "rainbond.io",
                "versions": [{"groupVersion": "rainbond.io/v1alpha1", "version": "v1alpha1"}],
                "preferredVersion": {"groupVersion": "rainbond.io/v1alpha1", "version": "v1alpha1"},
            }]
            resources = {
                "/api/v1": [
                    {"name": name, "kind": kind, "namespaced": namespaced, "verbs": ["get", "list"]}
                    for name, kind, namespaced in [("pods", "Pod", True), ("nodes", "Node", False)]
                ],
                "/apis/rainbond.io/v1alpha1": [{
                    "name": "rbdcomponents", "kind": "RbdComponent", "namespaced": True,
                    "verbs": ["get", "list", "patch"],
                }],
            }
            responses = {
                "/version": {"major": "1", "minor": "31", "gitVersion": "v1.31.0"},
                "/api": {"kind": "APIVersions", "versions": ["v1"]},
                "/apis": {"kind": "APIGroupList", "groups": groups},
                "/api/v1/nodes": {"apiVersion": "v1", "kind": "NodeList", "items": []},
                "/api/v1/namespaces/rbd-system/pods": {
                    "apiVersion": "v1", "kind": "PodList", "items": state.get("pods", []),
                },
            }
            if state.get("component") is not None:
                responses["/apis/rainbond.io/v1alpha1/namespaces/rbd-system/rbdcomponents/rbd-app-ui"] = state["component"]
            for endpoint, items in resources.items():
                responses[endpoint] = {
                    "apiVersion": "v1", "kind": "APIResourceList",
                    "groupVersion": endpoint.removeprefix("/apis/").removeprefix("/api/"),
                    "resources": items,
                }
            body = responses.get(path)
            code = 200 if body is not None else 404
            if body is None:
                body = {"apiVersion": "v1", "kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404}
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def flatten(documents):
    for document in documents:
        if not document:
            continue
        if document.get("kind") == "List":
            yield from document.get("items", [])
        else:
            yield document


class ChartRenderTest(unittest.TestCase):
    def render(self, values=None, *, upgrade=False, live=None, name="rainbond", chart=CHART):
        with tempfile.TemporaryDirectory(prefix="rainbond-chart-render-") as directory:
            directory = Path(directory)
            values_file = directory / "values.yaml"
            values_file.write_text(yaml.safe_dump(values or {}))
            config = directory / "kubeconfig"
            env = {key: value for key, value in os.environ.items() if key.lower() not in (
                "http_proxy", "https_proxy", "all_proxy", "kubeconfig")}
            env.update(KUBECONFIG=str(config), HELM_CACHE_HOME=str(directory / "cache"),
                       HELM_CONFIG_HOME=str(directory / "config"), HELM_DATA_HOME=str(directory / "data"))

            def execute(server):
                kubeconfig = {
                    "apiVersion": "v1", "kind": "Config", "current-context": "fixture",
                    "contexts": [{"name": "fixture", "context": {"cluster": "fixture", "user": "fixture"}}],
                    "clusters": [{"name": "fixture", "cluster": {"server": server}}],
                    "users": [{"name": "fixture", "user": {}}],
                }
                config.write_text(yaml.safe_dump(kubeconfig))
                config.chmod(0o600)
                command = ["helm", "template", name, str(chart), "--namespace", "rbd-system", "-f", str(values_file)]
                if upgrade:
                    command.append("--is-upgrade")
                if live is not None:
                    command.extend(["--dry-run=server", "--disable-openapi-validation"])
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, "helm rendering failed: " + result.stderr[-1200:])
                return list(flatten(yaml.safe_load_all(result.stdout)))

            if live is None:
                return execute("http://127.0.0.1:1")
            with api_server(live) as server:
                return execute(server)

    def migration(self, objects):
        return [obj for obj in objects if obj.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component") == COMPONENT]

    def jobs(self, objects):
        jobs = [obj for obj in self.migration(objects) if obj["kind"] == "Job"]
        self.assertTrue(jobs, "enabled Console migration must render at least one Job")
        return jobs

    def phase_job(self, objects, phase):
        jobs = [job for job in self.jobs(objects) if job["spec"]["template"]["spec"]["containers"][0]["args"] == [phase]]
        self.assertEqual(1, len(jobs), "render exactly one Job for phase " + phase)
        return jobs[0]

    def environment(self, job):
        return {item["name"]: item.get("value", item.get("valueFrom"))
                for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}

    def ready_pod(self, name="console", image_id="docker-pullable://" + DIGEST):
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "rbd-system", "labels": {"name": "rbd-app-ui"}},
            "spec": {"containers": [{"name": "rbd-app-ui", "image": "registry.example.test/rainbond:old"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "rbd-app-ui", "ready": True, "imageID": image_id}]},
        }

    def live(self, pods, mounts=None):
        if mounts is None:
            mounts = [{"name": "legacy-source", "mountPath": "/app/ui/console/views/app_overview.py"}]
        return {"component": {
            "apiVersion": "rainbond.io/v1alpha1", "kind": "RbdComponent",
            "metadata": {"name": "rbd-app-ui", "namespace": "rbd-system"},
            "spec": {"image": "registry.example.test/rainbond:old", "volumeMounts": mounts},
        }, "pods": pods}

    def test_new_install_keeps_console_native_without_migration_resources(self):
        objects = self.render()
        component = next(obj for obj in objects if obj["kind"] == "RbdComponent" and obj["metadata"]["name"] == "rbd-app-ui")
        self.assertNotIn("volumeMounts", component["spec"])
        self.assertNotIn("volumes", component["spec"])
        self.assertEqual([], self.migration(objects))

    def test_upgrade_prepares_before_apply_and_uses_same_backup(self):
        objects = self.render(upgrade=True)
        prepare, apply = self.phase_job(objects, "prepare"), self.phase_job(objects, "apply")
        self.assertEqual(2, len(self.jobs(objects)))
        self.assertEqual("pre-upgrade", prepare["metadata"]["annotations"]["helm.sh/hook"])
        self.assertEqual("post-upgrade", apply["metadata"]["annotations"]["helm.sh/hook"])
        self.assertEqual(self.environment(prepare)["BACKUP_CONFIGMAP"], self.environment(apply)["BACKUP_CONFIGMAP"])
        self.assertEqual("", self.environment(prepare)["BASELINE_IMAGE"])
        self.assertFalse(prepare["spec"]["template"]["spec"].get("initContainers"))

    def test_disabled_ui_or_migration_produces_no_hook_resources(self):
        for setting in [{"enable": False}, {"sourceMountMigration": {"enabled": False}}]:
            with self.subTest(setting=setting):
                self.assertEqual([], self.migration(self.render({"Component": {"rbd_app_ui": setting}}, upgrade=True)))

    def test_hook_dependencies_are_reusable_and_ordered_before_prepare(self):
        resources = self.migration(self.render(upgrade=True))
        kinds = {obj["kind"] for obj in resources}
        self.assertTrue({"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Job"}.issubset(kinds))
        prepare = self.phase_job(resources, "prepare")
        prepare_weight = int(prepare["metadata"]["annotations"]["helm.sh/hook-weight"])
        for obj in resources:
            annotations = obj["metadata"]["annotations"]
            if obj["kind"] == "Job":
                self.assertEqual({"before-hook-creation", "hook-succeeded"}, set(annotations["helm.sh/hook-delete-policy"].split(",")))
            else:
                self.assertEqual("pre-upgrade", annotations["helm.sh/hook"])
                self.assertEqual("before-hook-creation", annotations["helm.sh/hook-delete-policy"])
                self.assertLess(int(annotations["helm.sh/hook-weight"]), prepare_weight)

    def test_permissions_cannot_read_secrets_or_patch_other_components(self):
        resources = self.migration(self.render(upgrade=True))
        self.assertFalse(any(obj["kind"] in ("ClusterRole", "ClusterRoleBinding") for obj in resources))
        role = next(obj for obj in resources if obj["kind"] == "Role")
        rules = role["rules"]
        self.assertEqual(4, len(rules))
        for rule in rules:
            self.assertNotIn("*", rule["verbs"])
            self.assertNotIn("secrets", rule["resources"])
            if rule["resources"] == ["rbdcomponents"]:
                self.assertEqual(["rainbond.io"], rule["apiGroups"])
                self.assertEqual(["rbd-app-ui"], rule["resourceNames"])
                self.assertEqual({"get", "patch"}, set(rule["verbs"]))
            elif rule["resources"] == ["configmaps"]:
                self.assertEqual({"get", "create"}, set(rule["verbs"]))
            elif rule["resources"] == ["pods"]:
                self.assertEqual(["pods"], rule["resources"])
                self.assertEqual({"get", "list"}, set(rule["verbs"]))
            else:
                self.assertEqual(["batch"], rule["apiGroups"])
                self.assertEqual(["jobs"], rule["resources"])
                self.assertEqual(["get"], rule["verbs"])
                post_name = self.phase_job(resources, "apply")["metadata"]["name"]
                self.assertEqual({post_name, post_name.replace("-post-", "-pre-")}, set(rule["resourceNames"]))

    def test_prepare_inspects_unique_ready_digest_without_token_in_init(self):
        objects = self.render(upgrade=True, live=self.live([self.ready_pod()]))
        prepare = self.phase_job(objects, "prepare")
        pod = prepare["spec"]["template"]["spec"]
        self.assertEqual(DIGEST, self.environment(prepare)["BASELINE_IMAGE"])
        init = pod["initContainers"]
        self.assertEqual(1, len(init))
        self.assertEqual(DIGEST, init[0]["image"])
        self.assertEqual(["inspect", "--output=/native-baseline/files.json"], init[0]["args"])
        self.assertEqual({"script", "native-baseline"}, {mount["name"] for mount in init[0]["volumeMounts"]})
        self.assertFalse(any(mount["mountPath"].startswith("/var/run/secrets") for mount in init[0]["volumeMounts"]))
        self.assertEqual([{"name": "BASELINE_IMAGE", "value": DIGEST}], init[0].get("env"))
        self.assertEqual("/native-baseline/files.json", self.environment(prepare)["BASELINE_FILE"])

    def test_main_container_identity_matches_runtime_image_proof_contract(self):
        for job in self.jobs(self.render(upgrade=True)):
            self.assertEqual("migration", job["spec"]["template"]["spec"]["containers"][0]["name"])

    def test_duplicate_ready_pods_with_same_digest_are_a_single_baseline(self):
        objects = self.render(upgrade=True, live=self.live([self.ready_pod("one"), self.ready_pod("two", DIGEST)]))
        self.assertEqual(DIGEST, self.environment(self.phase_job(objects, "prepare"))["BASELINE_IMAGE"])

    def test_ready_console_without_source_overrides_does_not_inspect_the_old_image(self):
        for mounts in [[], [{"name": "settings", "mountPath": "/etc/console"}],
                       [{"name": "data", "mountPath": "/app/ui/console/config"}],
                       [{"name": "backup", "mountPath": "/app/ui/console/views/app_overview.py.backup"}]]:
            with self.subTest(mounts=len(mounts)):
                prepare = self.phase_job(self.render(upgrade=True, live=self.live([self.ready_pod()], mounts)), "prepare")
                self.assertEqual("", self.environment(prepare)["BASELINE_IMAGE"])
                self.assertFalse(prepare["spec"]["template"]["spec"].get("initContainers"))

    def test_source_parent_directories_and_normalized_paths_require_baseline_inspection(self):
        for path in ["/app/ui/console", "/app/ui", "/",
                     "/app/ui/console/views/../views/app_overview.py"]:
            with self.subTest(path=path):
                mounts = [{"name": "legacy-source", "mountPath": path}]
                prepare = self.phase_job(self.render(upgrade=True, live=self.live([self.ready_pod()], mounts)), "prepare")
                self.assertEqual(DIGEST, self.environment(prepare)["BASELINE_IMAGE"])

    def test_untrusted_or_ambiguous_ready_image_never_becomes_inspection_image(self):
        unready = self.ready_pod()
        unready["status"]["conditions"][0]["status"] = "False"
        deleting = self.ready_pod()
        deleting["metadata"]["deletionTimestamp"] = "2026-11-24T01:00:00Z"
        other = self.ready_pod()
        other["metadata"]["labels"]["name"] = "unrelated"
        mismatch = self.ready_pod()
        mismatch["spec"]["containers"][0]["image"] = "different:tag"
        missing_container = self.ready_pod()
        missing_container["status"]["containerStatuses"] = []
        for pods in [[], [unready], [deleting], [other], [mismatch],
                     [self.ready_pod(image_id="sha256:" + "a" * 64)],
                     [self.ready_pod(image_id="containerd://sha256:" + "a" * 64)],
                     [missing_container], [self.ready_pod("known"), mismatch],
                     [self.ready_pod("one"), self.ready_pod("two", DIGEST.replace("a" * 64, "b" * 64))]]:
            with self.subTest(pods=len(pods)):
                prepare = self.phase_job(self.render(upgrade=True, live=self.live(pods)), "prepare")
                self.assertEqual("", self.environment(prepare)["BASELINE_IMAGE"])
                self.assertFalse(prepare["spec"]["template"]["spec"].get("initContainers"))

    def test_main_container_alone_gets_explicit_short_lived_token_projection(self):
        for job in self.jobs(self.render(upgrade=True)):
            pod = job["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertNotEqual("rainbond-operator", pod["serviceAccountName"])
            main = pod["containers"][0]
            mounts = {item["name"]: item for item in main["volumeMounts"]}
            self.assertEqual("/var/run/secrets/kubernetes.io/serviceaccount", mounts["api-access"]["mountPath"])
            self.assertTrue(mounts["api-access"]["readOnly"])
            projection = next(volume for volume in pod["volumes"] if volume["name"] == "api-access")["projected"]["sources"]
            tokens = [item["serviceAccountToken"] for item in projection if "serviceAccountToken" in item]
            self.assertEqual([{"path": "token", "expirationSeconds": 600}], tokens)
            self.assertTrue(any(item.get("configMap", {}).get("name") == "kube-root-ca.crt" for item in projection))

    def test_jobs_have_bounded_runtime_and_restricted_filesystem(self):
        for job in self.jobs(self.render(upgrade=True)):
            self.assertEqual(0, job["spec"]["backoffLimit"])
            self.assertEqual(300, job["spec"]["activeDeadlineSeconds"])
            self.assertGreaterEqual(job["spec"]["ttlSecondsAfterFinished"], 3600)
            pod = job["spec"]["template"]["spec"]
            self.assertEqual("Never", pod["restartPolicy"])
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(65532, pod["securityContext"]["runAsUser"])
            self.assertEqual(65532, pod["securityContext"]["fsGroup"])
            self.assertFalse(any("hostPath" in volume or "persistentVolumeClaim" in volume or "secret" in volume
                                 for volume in pod["volumes"]))
            for container in pod["containers"] + pod.get("initContainers", []):
                self.assertEqual(["python3", "-I", "-S", "-B", "/migration/migrate_console_source_mounts.py"], container["command"])
                security = container["securityContext"]
                self.assertFalse(security["allowPrivilegeEscalation"])
                self.assertTrue(security["readOnlyRootFilesystem"])
                self.assertEqual(["ALL"], security["capabilities"]["drop"])

    def test_script_is_packaged_verbatim_in_hook_configmap(self):
        obj = next(obj for obj in self.migration(self.render(upgrade=True)) if obj["kind"] == "ConfigMap")
        self.assertEqual((CHART / "files/migrate_console_source_mounts.py").read_text(),
                         obj["data"]["migrate_console_source_mounts.py"])

    def test_dev_components_and_jobs_always_pull_without_changing_fixed_images(self):
        objects = self.render({"Cluster": {"installVersion": "v6.11.0-dev"}}, upgrade=True)
        components = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "RbdComponent"}
        for name in ("rbd-app-ui", "rbd-api"):
            self.assertEqual("Always", components[name]["spec"]["imagePullPolicy"])
            self.assertTrue(components[name]["spec"]["image"].endswith(":v6.11.0-dev"))
        operator = next(obj for obj in objects if obj["kind"] == "Deployment")
        self.assertEqual("Always", operator["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"])
        for job in self.jobs(objects):
            container = job["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(components["rbd-app-ui"]["spec"]["image"], container["image"])
            self.assertEqual("Always", container["imagePullPolicy"])
        self.assertEqual("IfNotPresent", components["minio"]["spec"]["imagePullPolicy"])

    def test_stable_components_keep_configured_policy(self):
        for policy in ("IfNotPresent", "Always", "Never"):
            with self.subTest(policy=policy):
                objects = self.render({"Cluster": {"installVersion": "v6.11.0-release", "imagePullPolicy": policy}})
                components = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "RbdComponent"}
                for name in ("rbd-app-ui", "rbd-api"):
                    self.assertEqual(policy, components[name]["spec"]["imagePullPolicy"])
                operator = next(obj for obj in objects if obj["kind"] == "Deployment")
                self.assertEqual(policy, operator["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"])

    def test_upgrade_inherits_live_image_pull_secret_references_without_reading_secrets(self):
        pod = self.ready_pod()
        pod["spec"]["imagePullSecrets"] = [{"name": "existing-registry-reference"}]
        live = self.live([pod])
        for job in self.jobs(self.render(upgrade=True, live=live)):
            self.assertEqual([{"name": "existing-registry-reference"}],
                             job["spec"]["template"]["spec"]["imagePullSecrets"])
        self.assertTrue(any(path.endswith("/pods") for path in live["_requests"]))
        self.assertFalse(any("/secrets" in path for path in live["_requests"]))

    def test_new_registry_reference_precedes_inherited_references_and_deduplicates(self):
        pod = self.ready_pod()
        pod["spec"]["imagePullSecrets"] = [{"name": "old-reference"}, {"name": "new-reference"}]
        values = {"Component": {"rbd_app_ui": {"sourceMountMigration": {
            "imagePullSecrets": [{"name": "new-reference"}, {"name": "new-reference"}],
        }}}}
        live = self.live([pod])
        for job in self.jobs(self.render(values, upgrade=True, live=live)):
            self.assertEqual([{"name": "new-reference"}, {"name": "old-reference"}],
                             job["spec"]["template"]["spec"]["imagePullSecrets"])
        self.assertFalse(any("/secrets" in path for path in live["_requests"]))

    def test_upgrade_without_pull_references_does_not_invent_a_secret(self):
        for job in self.jobs(self.render(upgrade=True)):
            self.assertNotIn("imagePullSecrets", job["spec"]["template"]["spec"])

    def test_environment_is_scoped_and_names_fit_kubernetes_limits(self):
        release = "a" * 53
        objects = self.render(name=release, upgrade=True)
        resources = self.migration(objects)
        names = [obj["metadata"]["name"] for obj in resources]
        self.assertEqual(len(resources), len({(obj["kind"], obj["metadata"]["name"]) for obj in resources}))
        for name in names:
            self.assertLessEqual(len(name), 63)
            self.assertRegex(name, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        for job in self.jobs(objects):
            env = self.environment(job)
            self.assertEqual("rbd-system", env["NAMESPACE"])
            self.assertEqual({"fieldRef": {"fieldPath": "metadata.name"}}, env["POD_NAME"])
            self.assertEqual("1", env["RELEASE_REVISION"])
            self.assertLessEqual(len(env["BACKUP_CONFIGMAP"]), 63)

    def test_backup_name_is_revision_specific_even_for_long_release(self):
        with tempfile.TemporaryDirectory(prefix="rainbond-chart-revision-") as directory:
            chart = Path(directory) / "chart"
            shutil.copytree(CHART, chart, ignore=shutil.ignore_patterns(".git"))
            (chart / "templates/revision-probe.yaml").write_text('''apiVersion: v1
kind: ConfigMap
metadata:
  name: revision-probe
data:
  backup: {{ include "rainbond.consoleSourceMountBackupName" (dict "Release" (dict "Name" .Release.Name "Revision" .Values.testRevision)) | quote }}
''')
            names = []
            for revision in ("1", "9223372036854775807"):
                objects = self.render({"testRevision": revision}, chart=chart, name="a" * 53)
                name = next(obj for obj in objects if obj["metadata"]["name"] == "revision-probe")["data"]["backup"]
                self.assertLessEqual(len(name), 63)
                names.append(name)
            self.assertNotEqual(*names)


if __name__ == "__main__":
    unittest.main()
