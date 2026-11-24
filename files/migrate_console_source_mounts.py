#!/usr/bin/env python3
"""Retire a reviewed legacy Console source bundle during a Helm upgrade.

This script runs in the Console image without importing Django. Only the native
file fingerprints below opt an image into this one-time migration. ConfigMap
contents, service account credentials and complete CRs are never logged.
"""

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
import posixpath
import ssl
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler


NATIVE_BUNDLE = {
    "console/services/app_actions/app_manage.py": "d16ef4fd01f8ab0288e8cfb3af1737d39174f475f92427109b5019506d5fc873",
    "console/views/app_overview.py": "07f70f585db27cc0f0c798f8540f4b04dd9692ccb4b4e3a6ddddebc25c17c3cc",
    "console/urls/__init__.py": "7018d62320d5cbbf7304780f5f8a657fa71eef9652a996e2b80c988ec41db30c",
    "console/utils/vm_vnc_proxy.py": "d0c0b7e89aeae6eafeb6a01d82c4ff86cb7349d428fe16596112999c1ad9c909",
    "console/views/vm_vnc.py": "970198be32705be67ea1a6b480cfe422a02ce21c938c3c0b9dbd947af102cc47",
    "console/utils/vm_vnc_proxy_relay.py": "e9f81766ecf96f7b7d1f4fbc1fac06b8dd649546ed6f963a61f218a6f8fbdc28",
}
LEGACY_HASHES = {name: {value} for name, value in NATIVE_BUNDLE.items()}
LEGACY_HASHES["console/services/app_actions/app_manage.py"].add(
    "7a9a9e938390a91b5fdbe8dc777908c7abad73a3f68e585913b9048000995c55")
LEGACY_HASHES["console/views/app_overview.py"].add(
    "1ece436b8259c5139a388781c39a74ad42e18e0794b4f9ff83550958f0812400")


class MigrationError(Exception):
    """An unsafe or unverified migration; messages contain no source contents."""


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def native_files(root=Path("/app/ui")):
    result = {}
    for relative in NATIVE_BUNDLE:
        file = Path(root) / relative
        result[relative] = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
    return result


def verify_native(files):
    require(files == NATIVE_BUNDLE,
            "Console native files do not match the reviewed migration bundle; retain the source mounts and review this image")


def array_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compact_backup(component, plan):
    spec = component["spec"]
    # Unknown volume options can contain private configuration. Persist only
    # retired source references and array fingerprints, never unrelated entries.
    return {
        "previous_mounts_digest": array_digest(spec["volumeMounts"]),
        "previous_volumes_digest": array_digest(spec["volumes"]),
        "after_mounts_digest": array_digest(plan["mounts"]),
        "after_volumes_digest": array_digest(plan["volumes"]),
        "retired_mounts": [{"index": index, "value": mount} for index, mount in enumerate(spec["volumeMounts"])
                           if mount not in plan["mounts"]],
        "retired_volumes": [{"index": index, "value": {"name": volume["name"], "configMap": volume["configMap"]}}
                            for index, volume in enumerate(spec["volumes"]) if volume not in plan["volumes"]],
        "removed_paths": plan["removed_paths"], "source_ids": plan["source_ids"],
        "source_fingerprints": plan["source_fingerprints"],
    }


def normalized_digest(value):
    value = str(value or "").removeprefix("docker-pullable://").removeprefix("containerd://").removeprefix("docker://")
    digest = value.rsplit("@", 1)[-1]
    require(digest.startswith("sha256:") and len(digest) == 71
            and all(c in "0123456789abcdef" for c in digest[7:]), "A verified immutable image digest is required")
    return digest


def mount_covers(mount_path, files):
    normalized = posixpath.normpath(mount_path)
    return any(file == normalized or file.startswith(normalized.rstrip("/") + "/") for file in files)


def source_paths(component):
    paths = {"/app/ui/" + name for name in NATIVE_BUNDLE}
    return [mount for mount in component.get("spec", {}).get("volumeMounts", [])
            if mount_covers(mount.get("mountPath", ""), paths)]


def plan_migration(component, configmap_getter, native_hashes):
    if not component or not source_paths(component):
        return None
    verify_native(native_hashes)
    spec = component["spec"]
    mounts, volumes = spec.get("volumeMounts", []), spec.get("volumes", [])
    by_volume = {volume["name"]: volume for volume in volumes}
    require(len(by_volume) == len(volumes), "Duplicate volume names require manual review")
    wanted = {"/app/ui/" + name: name for name in NATIVE_BUNDLE}
    removed, identities, fingerprints, configmaps = [], {}, {}, {}
    for mount in mounts:
        mount_path = posixpath.normpath(mount.get("mountPath", ""))
        relative = wanted.get(mount_path)
        if relative is None:
            # An enclosing custom directory may hide the verified image files.
            require(not mount_covers(mount_path, wanted),
                    "An enclosing custom source mount requires compatibility review")
            continue
        require(mount.get("readOnly") is True and mount.get("subPath") and not mount.get("subPathExpr"),
                "A source mount has unreviewed mount options")
        volume = by_volume.get(mount.get("name"), {})
        reference = volume.get("configMap") or {}
        name = reference.get("name")
        require(bool(name), "A source mount is not backed by a reviewed ConfigMap")
        if name not in configmaps:
            configmaps[name] = configmap_getter(name)
        configmap = configmaps[name]
        metadata = configmap.get("metadata", {})
        require(configmap.get("immutable") is True and metadata.get("name") == name and metadata.get("uid")
                and metadata.get("namespace") == component.get("metadata", {}).get("namespace"),
                "A source ConfigMap is mutable or its identity is unverified")
        key = mount["subPath"]
        if reference.get("items"):
            keys = [item["key"] for item in reference["items"] if item.get("path") == key]
            require(len(keys) == 1, "A source ConfigMap key mapping is ambiguous")
            key = keys[0]
        content = configmap.get("data", {}).get(key)
        require(isinstance(content, str), "A reviewed source key is missing")
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        require(fingerprint in LEGACY_HASHES[relative],
                "A source file contains an unknown customization; no mounts were removed")
        removed.append(mount)
        identities[name] = metadata["uid"]
        fingerprints[mount_path] = fingerprint
    remaining = [mount for mount in mounts if mount not in removed]
    used = {mount["name"] for mount in remaining}
    retired = {mount["name"] for mount in removed}
    return {
        "mounts": remaining,
        "volumes": [volume for volume in volumes if volume["name"] not in retired or volume["name"] in used],
        "removed_paths": sorted(posixpath.normpath(mount["mountPath"]) for mount in removed),
        "source_ids": identities,
        "source_fingerprints": fingerprints,
    }


def build_patch(component, plan):
    metadata, spec = component["metadata"], component["spec"]
    require(metadata.get("uid") and metadata.get("resourceVersion") and spec.get("image"),
            "The Console CR identity or version is missing")
    return [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/spec/image", "value": spec["image"]},
        {"op": "test", "path": "/spec/volumeMounts", "value": spec["volumeMounts"]},
        {"op": "test", "path": "/spec/volumes", "value": spec["volumes"]},
        {"op": "replace", "path": "/spec/volumeMounts", "value": copy.deepcopy(plan["mounts"])},
        {"op": "replace", "path": "/spec/volumes", "value": copy.deepcopy(plan["volumes"])},
    ]


def active_pods(client):
    return [pod for pod in client.get_pods() if not pod.get("metadata", {}).get("deletionTimestamp")
            and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]


def ready_image_digests(pods):
    require(bool(pods), "No running Console Pod is available for image verification")
    digests = set()
    for pod in pods:
        statuses = [status for status in pod.get("status", {}).get("containerStatuses", [])
                    if status.get("name") == "rbd-app-ui"]
        pod_ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in pod.get("status", {}).get("conditions", []))
        require(pod.get("status", {}).get("phase") == "Running" and pod_ready
                and len(statuses) == 1 and statuses[0].get("ready"),
                "Console is not ready; wait for its rollout before migrating source mounts")
        digests.add(normalized_digest(statuses[0].get("imageID")))
    return digests


def verify_pod_image_references(pods, image):
    for pod in pods:
        containers = [container for container in pod.get("spec", {}).get("containers", [])
                      if container.get("name") == "rbd-app-ui"]
        require(len(containers) == 1 and containers[0].get("image") == image,
                "The running Console Pod image reference does not match the CR image")


def verify_runtime_mounts(pods, component, plan):
    expected = {posixpath.normpath(mount["mountPath"]): mount
                for mount in component["spec"].get("volumeMounts", [])}
    volumes = {volume["name"]: volume for volume in component["spec"].get("volumes", [])}
    for pod in pods:
        actual_volumes = {volume["name"]: volume for volume in pod.get("spec", {}).get("volumes", [])}
        for container in pod.get("spec", {}).get("containers", []):
            if container.get("name") != "rbd-app-ui":
                continue
            for mount in container.get("volumeMounts", []):
                path = posixpath.normpath(mount.get("mountPath", ""))
                if not mount_covers(path, plan["removed_paths"]):
                    continue
                wanted = expected.get(path)
                require(wanted and all(mount.get(key) == wanted.get(key)
                                       for key in ("name", "readOnly", "subPath", "subPathExpr")),
                        "The running Console has an unreviewed source mount; no mounts were removed")
                original = volumes.get(mount.get("name"), {}).get("configMap", {})
                actual = actual_volumes.get(mount.get("name"), {}).get("configMap", {})
                require(original and all(actual.get(key) == original.get(key) for key in ("name", "items")),
                        "The running Console source volume differs from the reviewed CR")


def prepare(client, target_image, backup_name, revision, baseline_proof, native_hashes=None):
    component = client.get_component()
    if not component or not source_paths(component):
        return {"status": "noop"}
    hashes = native_files() if native_hashes is None else native_hashes
    plan = plan_migration(component, client.get_configmap, hashes)
    verify_native((baseline_proof or {}).get("files"))
    baseline_digest = normalized_digest((baseline_proof or {}).get("image"))
    pods = active_pods(client)
    verify_pod_image_references(pods, component["spec"]["image"])
    require(ready_image_digests(pods) == {baseline_digest},
            "The Console baseline changed after its native-image inspection")
    payload = {
        "version": 1, "revision": str(revision), "target_image": target_image,
        "target_digest": normalized_digest(client.get_own_image_id()),
        "baseline_digest": baseline_digest, "native_files": hashes,
        "component_uid": component["metadata"]["uid"],
        "previous_image": component["spec"]["image"],
        "source_backup": compact_backup(component, plan),
    }
    client.create_backup(backup_name, payload)
    return {"status": "prepared", "removed_paths": plan["removed_paths"]}


def wait_for_rollout(client, digest, plan):
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        pods = active_pods(client)
        try:
            ready = ready_image_digests(pods) == {digest}
        except MigrationError:
            ready = False
        component = client.get_component()
        if component:
            require(not any(mount_covers(mount.get("mountPath", ""), plan["removed_paths"])
                            for mount in component.get("spec", {}).get("volumeMounts", [])),
                    "Source mounts reappeared in the CR; inspect its configuration owner and retained backup")
            require((not plan.get("component_uid") or component["metadata"].get("uid") == plan["component_uid"])
                    and (not plan.get("target_image") or component["spec"].get("image") == plan["target_image"]),
                    "Console identity or image changed during the migrated rollout")
            verify_pod_image_references(pods, component["spec"]["image"])
        else:
            raise MigrationError("The Console CR disappeared during the migrated rollout")
        desired = max(1, int(component.get("spec", {}).get("replicas", 1))) if component else 1
        no_old_mounts = all(not any(
            mount_covers(mount.get("mountPath", ""), plan["removed_paths"])
            for container in pod.get("spec", {}).get("containers", []) if container.get("name") == "rbd-app-ui"
            for mount in container.get("volumeMounts", [])) for pod in pods)
        if ready and len(pods) >= desired and no_old_mounts:
            return
        time.sleep(2)
    raise MigrationError("Console did not become ready with the migrated mounts before the deadline")


def restore_after_failure(client, before, plan):
    current = client.get_component()
    require(current and current["metadata"]["uid"] == before["metadata"]["uid"]
            and current["spec"]["image"] == before["spec"]["image"]
            and current["spec"].get("volumeMounts") == plan["mounts"]
            and current["spec"].get("volumes") == plan["volumes"],
            "Concurrent configuration changed; mounts were not restored automatically; use the retained migration backup")
    sources = {}
    for name, uid in plan["source_ids"].items():
        source = client.get_configmap(name)
        sources[name] = source
        require(source.get("immutable") is True and source.get("metadata", {}).get("uid") == uid,
                "A source ConfigMap changed; mounts were not restored automatically; use the retained migration backup")
    require(plan_migration(before, sources.__getitem__, NATIVE_BUNDLE) == plan,
            "Source compatibility changed; mounts were not restored automatically")
    restored = {"mounts": before["spec"]["volumeMounts"], "volumes": before["spec"]["volumes"]}
    client.patch_component(build_patch(current, restored))


def wait_for_target_image(client, target_image, digest, baseline_digest, allow_baseline=False):
    deadline = time.monotonic() + 60
    while True:
        pods = active_pods(client)
        try:
            actual = ready_image_digests(pods)
        except MigrationError:
            actual = set()
        if actual == {digest}:
            verify_pod_image_references(pods, target_image)
            return True
        if allow_baseline and actual == {baseline_digest}:
            # A reused development tag may leave the old Pod unchanged until
            # retiring the source mounts itself triggers the rollout. Both
            # native bundles were verified; the final rollout must still use
            # the target digest. Always-pull is rechecked immediately before CAS.
            verify_pod_image_references(pods, target_image)
            return False
        require(actual <= {digest, baseline_digest},
                "The running Console and migration Job use different image digests; no mounts were removed")
        if time.monotonic() >= deadline:
            raise MigrationError("Console did not finish switching to the target image; no mounts were removed")
        time.sleep(2)


def apply(client, target_image, backup_name, revision, native_hashes=None, waiter=None):
    component = client.get_component()
    if not component:
        return {"status": "noop"}
    backup = client.get_backup(backup_name)
    needs_migration = bool(source_paths(component))
    if not needs_migration and not backup:
        return {"status": "noop"}
    require(component["spec"].get("image") == target_image,
            "Console CR image does not match the requested Helm target")
    hashes = native_files() if native_hashes is None else native_hashes
    verify_native(hashes)
    if needs_migration:
        plan = plan_migration(component, client.get_configmap, hashes)
    else:
        recorded = backup.get("source_backup", {})
        require(array_digest(component["spec"].get("volumeMounts")) == recorded.get("after_mounts_digest")
                and array_digest(component["spec"].get("volumes")) == recorded.get("after_volumes_digest"),
                "Configuration changed after migration; inspect the retained backup")
        plan = {"mounts": component["spec"]["volumeMounts"], "volumes": component["spec"]["volumes"],
                **{key: recorded[key] for key in ("removed_paths", "source_ids", "source_fingerprints")}}
    require(backup and backup.get("version") == 1 and backup.get("revision") == str(revision)
            and backup.get("target_image") == target_image and backup.get("native_files") == hashes
            and backup.get("component_uid") == component["metadata"]["uid"]
            and (not needs_migration or backup.get("source_backup") == compact_backup(component, plan)),
            "The verified pre-upgrade backup does not match the current source mounts; rerun the upgrade")
    target_digest = normalized_digest(client.get_own_image_id())
    require(target_digest == backup["target_digest"], "The target image changed between upgrade hooks")
    allow_baseline = (needs_migration and backup.get("previous_image") == target_image
                      and component["spec"].get("imagePullPolicy") == "Always")
    target_ready = wait_for_target_image(client, target_image, target_digest, backup["baseline_digest"], allow_baseline)
    if not needs_migration:
        (waiter or wait_for_rollout)(client, target_digest, dict(
            plan, component_uid=component["metadata"]["uid"], target_image=target_image))
        return {"status": "already-migrated", "removed_paths": plan["removed_paths"]}
    # Status updates during the image rollout also increment resourceVersion.
    # Refresh it, but never accept a changed source configuration or image.
    current = client.get_component()
    require(current and current["metadata"]["uid"] == component["metadata"]["uid"]
            and current["spec"].get("image") == target_image
            and current["spec"].get("volumeMounts") == component["spec"].get("volumeMounts")
            and current["spec"].get("volumes") == component["spec"].get("volumes"),
            "Console configuration changed while waiting for the image; no mounts were removed")
    component = current
    verify_runtime_mounts(active_pods(client), component, plan)
    # Immutable source objects must still have the identity reviewed in preflight.
    for name, uid in plan["source_ids"].items():
        require(client.get_configmap(name).get("metadata", {}).get("uid") == uid,
                "A source ConfigMap was replaced during migration")
    # Leave time for both rollout verification and a conditional restoration.
    # Test clients need no Job clock; the production client always enforces it.
    if hasattr(client, "ensure_time_budget"):
        client.ensure_time_budget(150)
    current = client.get_component()
    require(current and current["metadata"]["uid"] == component["metadata"]["uid"]
            and current["spec"].get("image") == target_image
            and current["spec"].get("volumeMounts") == component["spec"].get("volumeMounts")
            and current["spec"].get("volumes") == component["spec"].get("volumes")
            and (target_ready or current["spec"].get("imagePullPolicy") == "Always"),
            "Console configuration or pull policy changed before migration; no mounts were removed")
    component = current
    before = copy.deepcopy(component)
    try:
        client.patch_component(build_patch(component, plan))
        (waiter or wait_for_rollout)(client, target_digest, dict(
            plan, component_uid=component["metadata"]["uid"], target_image=target_image))
    except Exception:
        current = client.get_component()
        if (current and current.get("metadata", {}).get("uid") == before["metadata"]["uid"]
                and current.get("spec", {}).get("image") == before["spec"]["image"]
                and current["spec"].get("volumeMounts") == before["spec"]["volumeMounts"]
                and current["spec"].get("volumes") == before["spec"]["volumes"]):
            raise MigrationError("Migration did not complete; original mounts are present and the verified backup is retained") from None
        restore_after_failure(client, before, plan)
        raise MigrationError("Console rollout failed; previous mounts were restored; the migration backup is retained") from None
    return {"status": "migrated", "removed_paths": plan["removed_paths"]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KubernetesClient:
    def __init__(self, namespace, pod_name):
        credentials = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self._token = (credentials / "token").read_text().strip()
        self._opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=str(credentials / "ca.crt"))))
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self._base = "https://" + host + ":" + port
        self._core = "/api/v1/namespaces/" + quote(namespace, safe="")
        self._component = "/apis/rainbond.io/v1alpha1/namespaces/" + quote(namespace, safe="") + "/rbdcomponents/rbd-app-ui"
        self._pod_name = pod_name
        self.namespace = namespace

    def _request(self, method, path, body=None, content_type="application/json", missing_ok=False):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self._base + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + self._token, "Content-Type": content_type})
        try:
            with self._opener.open(request, timeout=8) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            # Never include server response bodies, source data or request headers.
            raise MigrationError("Kubernetes API " + method + " failed with HTTP " + str(error.code)) from None
        except (URLError, TimeoutError, ValueError):
            raise MigrationError("Kubernetes API request failed; no credentials or response contents were logged") from None

    def get_component(self):
        return self._request("GET", self._component, missing_ok=True)

    def get_configmap(self, name):
        return self._request("GET", self._core + "/configmaps/" + quote(name, safe=""))

    def create_backup(self, name, payload):
        old = self._request("GET", self._core + "/configmaps/" + quote(name, safe=""), missing_ok=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if old is not None:
            require(old.get("immutable") is True and old.get("data", {}).get("migration.json") == encoded,
                    "The retained backup differs from this preflight; use a new Helm revision")
            return
        self._request("POST", self._core + "/configmaps", {
            "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": name, "namespace": self.namespace,
                         "labels": {"app.kubernetes.io/component": "console-source-migration-backup"}},
            "data": {"migration.json": encoded},
        })

    def get_backup(self, name):
        obj = self._request("GET", self._core + "/configmaps/" + quote(name, safe=""), missing_ok=True)
        if obj is None:
            return None
        require(obj.get("immutable") is True, "The migration backup is mutable")
        try:
            data = json.loads(obj["data"]["migration.json"])
        except (KeyError, ValueError):
            raise MigrationError("The migration backup is invalid") from None
        require(isinstance(data, dict), "The migration backup is invalid")
        return data

    def get_pods(self):
        return self._request("GET", self._core + "/pods?labelSelector=name%3Drbd-app-ui")["items"]

    def get_own_image_id(self):
        deadline = time.monotonic() + 10
        while True:
            pod = self._request("GET", self._core + "/pods/" + quote(self._pod_name, safe=""))
            statuses = [status for status in pod.get("status", {}).get("containerStatuses", [])
                        if status.get("name") == "migration"]
            require(len(statuses) <= 1, "The migration Job image identity is ambiguous")
            if statuses and statuses[0].get("imageID"):
                normalized_digest(statuses[0]["imageID"])
                return statuses[0]["imageID"]
            if time.monotonic() >= deadline:
                raise MigrationError("The migration Job image identity was not reported before the deadline")
            time.sleep(0.5)

    def ensure_time_budget(self, minimum_seconds):
        pod = self._request("GET", self._core + "/pods/" + quote(self._pod_name, safe=""))
        owners = [owner for owner in pod.get("metadata", {}).get("ownerReferences", [])
                  if owner.get("kind") == "Job" and owner.get("controller") is True]
        require(len(owners) == 1 and owners[0].get("uid") and owners[0].get("name"),
                "The migration Job owner identity is missing")
        job = self._request("GET", "/apis/batch/v1/namespaces/" + quote(self.namespace, safe="")
                            + "/jobs/" + quote(owners[0]["name"], safe=""))
        require(job.get("metadata", {}).get("uid") == owners[0]["uid"], "The migration Job identity changed")
        started = job.get("status", {}).get("startTime") or job.get("metadata", {}).get("creationTimestamp")
        try:
            elapsed = time.time() - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
            limit = int(job["spec"]["activeDeadlineSeconds"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MigrationError("The migration Job deadline is unverified; no mounts were removed") from None
        require(0 <= elapsed and limit - elapsed >= minimum_seconds,
                "Not enough Job time remains for a safe rollout and restoration; no mounts were removed")

    def patch_component(self, patch):
        return self._request("PATCH", self._component, patch, "application/json-patch+json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["inspect", "prepare", "apply"])
    parser.add_argument("--output", default="/native-baseline/files.json")
    args = parser.parse_args()
    try:
        if args.phase == "inspect":
            Path(args.output).write_text(json.dumps({"image": os.environ.get("BASELINE_IMAGE"), "files": native_files()}))
            return
        client = KubernetesClient(os.environ["NAMESPACE"], os.environ["POD_NAME"])
        parameters = (client, os.environ["TARGET_IMAGE"], os.environ["BACKUP_CONFIGMAP"], os.environ["RELEASE_REVISION"])
        if args.phase == "prepare":
            file = Path("/native-baseline/files.json")
            proof = json.loads(file.read_text()) if file.exists() else None
            result = prepare(*parameters, proof)
        else:
            result = apply(*parameters)
    except (MigrationError, OSError, ValueError, KeyError) as error:
        # Only explicitly curated MigrationError messages are safe for operator logs.
        message = str(error) if isinstance(error, MigrationError) else "Migration configuration or native-file inspection failed"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
    print("Console source migration: " + result["status"])


if __name__ == "__main__":
    main()
