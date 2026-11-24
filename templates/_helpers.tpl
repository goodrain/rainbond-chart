{{/*
Detect containerd runtime path based on Kubernetes distribution
*/}}
{{- define "rainbond.containerdRuntimePath" -}}
{{- if .Values.Cluster.containerdRuntimePath -}}
  {{- .Values.Cluster.containerdRuntimePath -}}
{{- else -}}
  {{- $detectedPath := "/run/containerd" -}}
  {{- $nodes := (lookup "v1" "Node" "" "").items -}}
  {{- if $nodes -}}
    {{- range $nodes -}}
      {{- $kubeletVersion := .status.nodeInfo.kubeletVersion -}}
      {{- if or (contains "k3s" $kubeletVersion) (contains "rke2" $kubeletVersion) -}}
        {{- $detectedPath = "/var/run/k3s/containerd" -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- $detectedPath -}}
{{- end -}}
{{- end -}}

{{/* Mutable development images must not reuse a cached image from an older build. */}}
{{- define "rainbond.consoleUpgradeImagePullPolicy" -}}
{{- if contains "-dev" (toString .Values.Cluster.installVersion) -}}
Always
{{- else -}}
{{- .Values.Cluster.imagePullPolicy | default "IfNotPresent" -}}
{{- end -}}
{{- end -}}

{{/* Leave room for a 19-digit Helm revision while retaining release identity. */}}
{{- define "rainbond.consoleSourceMountMigrationName" -}}
{{- printf "%s-console-src-%s" (.Release.Name | trunc 15 | trimSuffix "-") (.Release.Name | sha256sum | trunc 8) -}}
{{- end -}}

{{- define "rainbond.consoleSourceMountBackupName" -}}
{{- printf "%s-backup-%v" (include "rainbond.consoleSourceMountMigrationName" .) .Release.Revision -}}
{{- end -}}

{{/* Pass only existing Secret references to the Pod; never read Secret data.
Explicit target-registry references come first, followed by the current Console. */}}
{{- define "rainbond.consoleSourceMountPullSecrets" -}}
{{- $migration := .Values.Component.rbd_app_ui.sourceMountMigration | default dict -}}
{{- $explicit := $migration.imagePullSecrets | default list -}}
{{- if not (kindIs "slice" $explicit) -}}
  {{- fail "sourceMountMigration.imagePullSecrets must be a list of Secret name references" -}}
{{- end -}}
{{- $seen := dict -}}
{{- $result := list -}}
{{- range $entry := $explicit -}}
  {{- if not (kindIs "map" $entry) -}}
    {{- fail "sourceMountMigration.imagePullSecrets entries must contain only a nonempty name" -}}
  {{- end -}}
  {{- if or (ne (len $entry) 1) (not (hasKey $entry "name")) -}}
    {{- fail "sourceMountMigration.imagePullSecrets entries must contain only a nonempty name" -}}
  {{- end -}}
  {{- $secretName := get $entry "name" -}}
  {{- if not (kindIs "string" $secretName) -}}
    {{- fail "sourceMountMigration.imagePullSecrets entries must contain only a nonempty name" -}}
  {{- end -}}
  {{- if or (gt (len $secretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" $secretName)) -}}
    {{- fail "sourceMountMigration.imagePullSecrets entries must contain only a valid Secret name" -}}
  {{- end -}}
  {{- if not (hasKey $seen $secretName) -}}
    {{- $_ := set $seen $secretName true -}}
    {{- $result = append $result (dict "name" $secretName) -}}
  {{- end -}}
{{- end -}}
{{- $pods := lookup "v1" "Pod" .Release.Namespace "" -}}
{{- range $pod := $pods.items | default list -}}
  {{- $metadata := $pod.metadata | default dict -}}
  {{- $labels := $metadata.labels | default dict -}}
  {{- $spec := $pod.spec | default dict -}}
  {{- if and (eq (get $labels "name") "rbd-app-ui") (not $metadata.deletionTimestamp) -}}
    {{- range $entry := $spec.imagePullSecrets | default list -}}
      {{- $secretName := $entry.name | default "" -}}
      {{- if and $secretName (not (hasKey $seen $secretName)) -}}
        {{- $_ := set $seen $secretName true -}}
        {{- $result = append $result (dict "name" $secretName) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if $result -}}{{ toYaml $result }}{{- end -}}
{{- end -}}

{{/* Inspect the old image without its source overrides. Unknown or mixed images
must not be guessed: prepare will require proof when legacy mounts are present. */}}
{{- define "rainbond.consoleSourceMountBaselineImage" -}}
{{- $component := lookup "rainbond.io/v1alpha1" "RbdComponent" .Release.Namespace "rbd-app-ui" -}}
{{- if $component -}}
  {{- $componentSpec := $component.spec | default dict -}}
  {{- $candidate := false -}}
  {{/* These paths match the migration script's reviewed NATIVE_BUNDLE. */}}
  {{- $sourceFiles := list
      "/app/ui/console/services/app_actions/app_manage.py"
      "/app/ui/console/views/app_overview.py"
      "/app/ui/console/urls/__init__.py"
      "/app/ui/console/utils/vm_vnc_proxy.py"
      "/app/ui/console/views/vm_vnc.py"
      "/app/ui/console/utils/vm_vnc_proxy_relay.py" -}}
  {{- range $mount := $componentSpec.volumeMounts | default list -}}
    {{- $path := clean ($mount.mountPath | default "") -}}
    {{- range $file := $sourceFiles -}}
      {{- if or (eq $file $path) (hasPrefix (printf "%s/" (trimSuffix "/" $path)) $file) -}}
        {{- $candidate = true -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if $candidate -}}
  {{- $images := dict -}}
  {{- $state := dict "invalid" false -}}
  {{- $pods := lookup "v1" "Pod" .Release.Namespace "" -}}
  {{- range $pod := $pods.items | default list -}}
    {{- $metadata := $pod.metadata | default dict -}}
    {{- $labels := $metadata.labels | default dict -}}
    {{- $status := $pod.status | default dict -}}
    {{- if and (eq (get $labels "name") "rbd-app-ui") (not $metadata.deletionTimestamp) (eq ($status.phase | default "") "Running") -}}
      {{- $ready := false -}}
      {{- range $condition := $status.conditions | default list -}}
        {{- if and (eq $condition.type "Ready") (eq $condition.status "True") -}}
          {{- $ready = true -}}
        {{- end -}}
      {{- end -}}
      {{- if $ready -}}
        {{- $spec := $pod.spec | default dict -}}
        {{- $matchingContainer := false -}}
        {{- range $container := $spec.containers | default list -}}
          {{- if and (eq $container.name "rbd-app-ui") (eq $container.image $componentSpec.image) -}}
            {{- $matchingContainer = true -}}
          {{- end -}}
        {{- end -}}
        {{- $identified := false -}}
        {{- if $matchingContainer -}}
          {{- range $container := $status.containerStatuses | default list -}}
            {{- if and (eq $container.name "rbd-app-ui") $container.ready -}}
              {{- $image := trimPrefix "docker-pullable://" ($container.imageID | default "") -}}
              {{- if regexMatch "^[^[:space:]]+@sha256:[a-f0-9]{64}$" $image -}}
                {{- $_ := set $images $image true -}}
                {{- $identified = true -}}
              {{- end -}}
            {{- end -}}
          {{- end -}}
        {{- end -}}
        {{- if not $identified -}}
          {{- $_ := set $state "invalid" true -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if and (not $state.invalid) (eq (len $images) 1) -}}
    {{- range $image, $_ := $images -}}{{ $image }}{{- end -}}
  {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}
