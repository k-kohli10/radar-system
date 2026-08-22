{{/*
Chart name, honouring nameOverride.
*/}}
{{- define "radar.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every object.
*/}}
{{- define "radar.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "radar.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: radar
{{- end -}}

{{/*
Selector labels for one service. Call with a dict:
  (dict "name" $serviceName "root" $)
The component label makes each service's selector unique within the release.
*/}}
{{- define "radar.selectorLabels" -}}
{{- $root := .root -}}
app.kubernetes.io/name: {{ include "radar.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ .name }}
{{- end -}}

{{/*
Fully qualified image reference for one service. Call with a dict:
  (dict "name" $serviceName "root" $)
A per-service `image` value overrides the computed default entirely.
*/}}
{{- define "radar.image" -}}
{{- $root := .root -}}
{{- $svc := (index $root.Values.services .name) | default dict -}}
{{- if $svc.image -}}
{{- $svc.image -}}
{{- else -}}
{{- $tag := $root.Values.image.tag | default $root.Chart.AppVersion -}}
{{- printf "%s/%s/radar-%s:%s" $root.Values.image.registry $root.Values.image.owner .name $tag -}}
{{- end -}}
{{- end -}}
