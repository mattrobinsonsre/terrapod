{{/*
Expand the name of the chart.
*/}}
{{- define "terrapod.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "terrapod.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "terrapod.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "terrapod.labels" -}}
helm.sh/chart: {{ include "terrapod.chart" . }}
{{ include "terrapod.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "terrapod.selectorLabels" -}}
app.kubernetes.io/name: {{ include "terrapod.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
API selector labels
*/}}
{{- define "terrapod.api.selectorLabels" -}}
{{ include "terrapod.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end }}

{{/*
Listener selector labels
*/}}
{{- define "terrapod.listener.selectorLabels" -}}
{{ include "terrapod.selectorLabels" . }}
app.kubernetes.io/component: listener
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "terrapod.serviceAccountName" -}}
{{- if .Values.api.serviceAccount.create }}
{{- default (include "terrapod.fullname" .) .Values.api.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.api.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the image tag, defaulting to appVersion
*/}}
{{- define "terrapod.api.image" -}}
{{- $tag := default .Chart.AppVersion .Values.api.image.tag -}}
{{- printf "%s:%s" .Values.api.image.repository $tag -}}
{{- end }}

{{/*
Get the runner namespace (defaults to release namespace)
*/}}
{{- define "terrapod.runnerNamespace" -}}
{{- default .Release.Namespace .Values.listener.runnerNamespace -}}
{{- end }}

{{/*
Web selector labels
*/}}
{{- define "terrapod.web.selectorLabels" -}}
{{ include "terrapod.selectorLabels" . }}
app.kubernetes.io/component: web
{{- end }}

{{/*
Get the web image tag, defaulting to appVersion
*/}}
{{- define "terrapod.web.image" -}}
{{- $tag := default .Chart.AppVersion .Values.web.image.tag -}}
{{- printf "%s:%s" .Values.web.image.repository $tag -}}
{{- end }}

{{/*
Validate storage configuration — exactly one backend must be configured.
*/}}
{{- define "terrapod.validateStorageConfig" -}}
{{- $backend := .Values.api.config.storage.backend -}}
{{- if not (or (eq $backend "s3") (eq $backend "azure") (eq $backend "gcs") (eq $backend "filesystem")) -}}
{{- fail (printf "Invalid storage backend: %s. Must be one of: s3, azure, gcs, filesystem" $backend) -}}
{{- end -}}
{{- if eq $backend "s3" -}}
  {{- if not .Values.api.config.storage.s3.bucket -}}
  {{- fail "storage.s3.bucket is required when backend is s3" -}}
  {{- end -}}
{{- end -}}
{{- if eq $backend "azure" -}}
  {{- if not .Values.api.config.storage.azure.account_name -}}
  {{- fail "storage.azure.account_name is required when backend is azure" -}}
  {{- end -}}
  {{- if not .Values.api.config.storage.azure.container_name -}}
  {{- fail "storage.azure.container_name is required when backend is azure" -}}
  {{- end -}}
{{- end -}}
{{- if eq $backend "gcs" -}}
  {{- if not .Values.api.config.storage.gcs.bucket -}}
  {{- fail "storage.gcs.bucket is required when backend is gcs" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
