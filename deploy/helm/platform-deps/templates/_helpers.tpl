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

{{/*
Generate-or-reuse a dev platform credential, so no password is committed to the
repo. Looks up the existing Secret's key and REUSES it (an upgrade must not rotate
a live credential — Postgres would lock out); generates a fresh random value only
on first install (and on a brand-new cluster). Dev/eval ONLY — production runs
external managed backends (deploy/examples/bring-your-own-backends), so these are
throwaway and never persisted anywhere but the cluster's own Secret.
Call: (include "platformdeps.genCred" (dict "root" $ "name" "radar-vault" "key" "root-token" "len" 32))
*/}}
{{- define "platformdeps.genCred" -}}
{{- $existing := (lookup "v1" "Secret" .root.Release.Namespace .name) -}}
{{- if and $existing $existing.data (hasKey $existing.data .key) -}}
{{- index $existing.data .key | b64dec -}}
{{- else -}}
{{- randAlphaNum (.len | int) -}}
{{- end -}}
{{- end -}}
