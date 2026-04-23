{{/*
Common labels applied to every resource in this chart.
*/}}
{{- define "k8s-debug-agents.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels (a stable subset used by Services/Deployments for selection).
*/}}
{{- define "k8s-debug-agents.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Fully-qualified image reference. Registry is optional (empty string = use image
name as-is, which is what Kind needs for locally-loaded images).

Usage: {{ include "k8s-debug-agents.image" (dict "image" .Values.images.dispatcher "root" .) }}
*/}}
{{- define "k8s-debug-agents.image" -}}
{{- $registry := .root.Values.images.registry -}}
{{- $image := .image -}}
{{- $tag := .root.Values.images.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $image $tag -}}
{{- else -}}
{{- printf "%s:%s" $image $tag -}}
{{- end -}}
{{- end -}}
