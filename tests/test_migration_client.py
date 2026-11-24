"""Exercise Kubernetes transport, identity and deadline guards without credentials."""

from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import build_opener


SPEC = importlib.util.spec_from_file_location(
    "migration_client_test_subject", Path(__file__).resolve().parents[1] / "files/migrate_console_source_mounts.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
DIGEST = "registry.example/rainbond@sha256:" + "a" * 64


class MemoryResponse(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class MemoryOpener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return MemoryResponse(value)


class MigrationClientTest(unittest.TestCase):
    def client(self, *responses):
        client = migration.KubernetesClient.__new__(migration.KubernetesClient)
        # An empty fixture never uses a real credential or opens a network socket.
        client._token = ""
        client._base = "https://kubernetes.example.invalid:443"
        client._core = "/api/v1/namespaces/qa"
        client._component = "/apis/rainbond.io/v1alpha1/namespaces/qa/rbdcomponents/rbd-app-ui"
        client._pod_name = "migration-pod"
        client.namespace = "qa"
        client._opener = MemoryOpener(*responses)
        return client

    def test_source_reads_and_json_patch_use_the_namespaced_api_without_secret_calls(self):
        client = self.client({"metadata": {}}, {"metadata": {}}, {"items": []}, {"spec": {}})
        client.get_component()
        client.get_configmap("source-map")
        client.get_pods()
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": "1"}]
        client.patch_component(patch)
        requests = [call[0] for call in client._opener.calls]
        self.assertEqual(["GET", "GET", "GET", "PATCH"], [request.method for request in requests])
        self.assertEqual(patch, json.loads(requests[-1].data))
        self.assertEqual("application/json-patch+json", requests[-1].get_header("Content-type"))
        self.assertTrue(all("/namespaces/qa/" in request.full_url and "/secrets" not in request.full_url
                            for request in requests))

    def test_http_error_does_not_include_source_body_or_response_headers(self):
        body = "unreviewed-configmap-source-fixture"
        client = self.client(HTTPError("https://example.invalid", 403, body, {"X-Fixture": body}, io.BytesIO(body.encode())))
        with self.assertRaises(migration.MigrationError) as caught:
            client.get_configmap("map")
        self.assertEqual("Kubernetes API GET failed with HTTP 403", str(caught.exception))
        self.assertNotIn(body, str(caught.exception))

    def test_missing_component_is_a_noop_but_other_api_errors_are_not_swallowed(self):
        client = self.client(HTTPError("https://example.invalid", 404, "missing", {}, None))
        self.assertIsNone(client.get_component())
        client = self.client(HTTPError("https://example.invalid", 500, "server fixture", {}, None))
        with self.assertRaises(migration.MigrationError):
            client.get_component()

    def test_network_errors_do_not_repeat_exception_contents(self):
        client = self.client(URLError("private upstream fixture"))
        with self.assertRaises(migration.MigrationError) as caught:
            client.get_pods()
        self.assertNotIn("private upstream fixture", str(caught.exception))

    def test_redirects_are_disabled_in_the_real_opener_handler_chain(self):
        opener = build_opener(migration.NoRedirect())
        handlers = [handler for handler in opener.handlers if isinstance(handler, migration.HTTPRedirectHandler)]
        self.assertEqual(1, len(handlers))
        self.assertIsNone(handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://example.invalid"))

    def test_own_image_identity_waits_for_the_kubelet_status_report(self):
        client = self.client({}, {"status": {"containerStatuses": [{"name": "migration", "imageID": DIGEST}]}})
        with mock.patch.object(migration.time, "sleep") as sleeping:
            self.assertEqual(DIGEST, client.get_own_image_id())
        sleeping.assert_called_once_with(0.5)
        self.assertEqual(2, len(client._opener.calls))

    def test_own_image_identity_fails_when_status_never_arrives_or_is_ambiguous(self):
        client = self.client({}, {})
        with mock.patch.object(migration.time, "monotonic", side_effect=[0, 0, 11]), mock.patch.object(migration.time, "sleep"):
            with self.assertRaises(migration.MigrationError):
                client.get_own_image_id()
        client = self.client({"status": {"containerStatuses": [
            {"name": "migration", "imageID": DIGEST}, {"name": "migration", "imageID": DIGEST}]}})
        with self.assertRaises(migration.MigrationError):
            client.get_own_image_id()

    def test_own_image_identity_does_not_retry_an_invalid_digest(self):
        client = self.client({"status": {"containerStatuses": [{"name": "migration", "imageID": "not-an-image-digest"}]}})
        with mock.patch.object(migration.time, "sleep") as sleeping:
            with self.assertRaises(migration.MigrationError):
                client.get_own_image_id()
        sleeping.assert_not_called()

    def job_objects(self, elapsed=20, uid="job-uid"):
        pod = {"metadata": {"ownerReferences": [{"kind": "Job", "controller": True, "name": "migration-post-2", "uid": "job-uid"}]}}
        began = datetime.fromtimestamp(1000 - elapsed, timezone.utc).isoformat()
        job = {"metadata": {"uid": uid}, "spec": {"activeDeadlineSeconds": 180}, "status": {"startTime": began}}
        return pod, job

    def test_job_budget_uses_the_real_job_start_and_owner_not_a_fresh_pod_clock(self):
        client = self.client(*self.job_objects())
        with mock.patch.object(migration.time, "time", return_value=1000):
            client.ensure_time_budget(110)
        request = client._opener.calls[-1][0]
        self.assertEqual("https://kubernetes.example.invalid:443/apis/batch/v1/namespaces/qa/jobs/migration-post-2", request.full_url)
        self.assertEqual("GET", request.method)

    def test_insufficient_or_unverified_job_budget_stops_before_any_patch(self):
        for elapsed, uid in [(80, "job-uid"), (-1, "job-uid"), (20, "replaced-job")]:
            with self.subTest(elapsed=elapsed, uid=uid):
                client = self.client(*self.job_objects(elapsed, uid))
                with mock.patch.object(migration.time, "time", return_value=1000):
                    with self.assertRaises(migration.MigrationError):
                        client.ensure_time_budget(110)
                self.assertTrue(all(request.method == "GET" for request, _ in client._opener.calls))
        client = self.client({"metadata": {"ownerReferences": []}})
        with self.assertRaises(migration.MigrationError):
            client.ensure_time_budget(110)

    def test_backups_are_immutable_and_do_not_overwrite_a_different_existing_revision(self):
        client = self.client(HTTPError("https://example.invalid", 404, "missing", {}, None), {})
        payload = {"revision": "2", "source_backup": {"removed_paths": ["/app/ui/console/views/example.py"]}}
        client.create_backup("reviewed-backup", payload)
        request = client._opener.calls[-1][0]
        body = json.loads(request.data)
        self.assertEqual("POST", request.method)
        self.assertTrue(body["immutable"])
        self.assertEqual(payload, json.loads(body["data"]["migration.json"]))
        client = self.client(body)
        client.create_backup("reviewed-backup", payload)
        self.assertEqual(1, len(client._opener.calls))
        client = self.client(body)
        with self.assertRaises(migration.MigrationError):
            client.create_backup("reviewed-backup", {"revision": "3"})
        self.assertEqual(1, len(client._opener.calls))


if __name__ == "__main__":
    unittest.main()
