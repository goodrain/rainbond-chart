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
