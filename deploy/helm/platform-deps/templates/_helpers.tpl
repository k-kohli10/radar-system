{{/*
Common labels for platform-deps objects. `component` is set by each template.
*/}}
{{- define "platformdeps.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: radar
app.kubernetes.io/tier: platform
{{- end -}}
