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
{{- with .Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
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
Runner Job service account name.
When runners.serviceAccount.create=true: defaults to "<fullname>-runner".
When create=false: uses runners.serviceAccount.name, or "default" if unset.
*/}}
{{- define "terrapod.runnerServiceAccountName" -}}
{{- if .Values.runners.serviceAccount.create }}
{{- default (printf "%s-runner" (include "terrapod.fullname" .)) .Values.runners.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.runners.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Listener service account name.
*/}}
{{- define "terrapod.listenerServiceAccountName" -}}
{{- if .Values.listener.serviceAccount.create }}
{{- default (printf "%s-listener" (include "terrapod.fullname" .)) .Values.listener.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.listener.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Listener credentials Secret name. Tied to the listener Deployment name so the
Secret follows the Deployment's lifecycle (rename/delete cleans up naturally)
and is unique per Helm release even if multiple releases share `listener.name`.
*/}}
{{- define "terrapod.listenerCredentialsSecretName" -}}
{{- printf "%s-listener-credentials" (include "terrapod.fullname" .) -}}
{{- end -}}

{{/*
Get the API image reference, defaulting tag to appVersion
*/}}
{{- define "terrapod.api.image" -}}
{{- $repo := .Values.api.image.repository -}}
{{- if .Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" .Values.global.imageRegistry (trimPrefix "ghcr.io/" $repo) -}}
{{- end -}}
{{- $tag := default .Chart.AppVersion .Values.api.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{/*
Get the listener image reference, defaulting tag to appVersion
*/}}
{{- define "terrapod.listener.image" -}}
{{- $repo := .Values.listener.image.repository -}}
{{- if .Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" .Values.global.imageRegistry (trimPrefix "ghcr.io/" $repo) -}}
{{- end -}}
{{- $tag := default .Chart.AppVersion .Values.listener.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{/*
Get the migrations image reference, defaulting tag to appVersion
*/}}
{{- define "terrapod.migrations.image" -}}
{{- $repo := .Values.migrations.image.repository -}}
{{- if .Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" .Values.global.imageRegistry (trimPrefix "ghcr.io/" $repo) -}}
{{- end -}}
{{- $tag := default .Chart.AppVersion .Values.migrations.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{/*
Get the runner image repository with registry override applied.
Used in the runner ConfigMap (not a full image:tag ref since the ConfigMap
has separate repository and tag fields).
*/}}
{{- define "terrapod.runner.imageRepo" -}}
{{- $repo := .Values.runners.image.repository -}}
{{- if .Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" .Values.global.imageRegistry (trimPrefix "ghcr.io/" $repo) -}}
{{- end -}}
{{- $repo -}}
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
Get the web image reference, defaulting tag to appVersion
*/}}
{{- define "terrapod.web.image" -}}
{{- $repo := .Values.web.image.repository -}}
{{- if .Values.global.imageRegistry -}}
{{- $repo = printf "%s/%s" .Values.global.imageRegistry (trimPrefix "ghcr.io/" $repo) -}}
{{- end -}}
{{- $tag := default .Chart.AppVersion .Values.web.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{/*
Pod anti-affinity block.
Usage: {{ include "terrapod.podAntiAffinity" (dict "enabled" .Values.api.podAntiAffinity.enabled "affinity" .Values.api.affinity "labels" (include "terrapod.api.selectorLabels" .)) }}
When .affinity is non-empty it is used as a full override. Otherwise, if
.enabled is true, auto-generates required node anti-affinity + preferred AZ
anti-affinity using the provided selector labels.
*/}}
{{- define "terrapod.podAntiAffinity" -}}
{{- if .affinity }}
      affinity:
        {{- toYaml .affinity | nindent 8 }}
{{- else if .enabled }}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  {{- .labels | nindent 18 }}
              topologyKey: kubernetes.io/hostname
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    {{- .labels | nindent 20 }}
                topologyKey: topology.kubernetes.io/zone
{{- end }}
{{- end -}}

{{/*
Web service account name.
*/}}
{{- define "terrapod.webServiceAccountName" -}}
{{- if .Values.web.serviceAccount.create }}
{{- default (printf "%s-web" (include "terrapod.fullname" .)) .Values.web.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.web.serviceAccount.name }}
{{- end }}
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
{{- if eq $backend "filesystem" -}}
  {{- if or (gt (int .Values.api.replicas) 1) .Values.api.autoscaling.enabled -}}
  {{- fail "Filesystem storage backend does not support multiple API replicas. Set api.replicas=1 and api.autoscaling.enabled=false, or use a cloud storage backend (s3, azure, gcs)." -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/*
Validate ingress requires web UI to be enabled.
*/}}
{{- define "terrapod.validateIngressWeb" -}}
{{- if and .Values.ingress.enabled (not .Values.web.enabled) -}}
{{- fail "Ingress is enabled but web.enabled is false. The Ingress routes to the web frontend — set web.enabled=true or disable the Ingress." -}}
{{- end -}}
{{- end -}}

{{/*
Validate the optional webhook Ingress:
  - hostname is required when enabled (every public-internet caller resolves it)
  - web.enabled is required (the webhook Ingress routes to the web BFF, same
    as the management Ingress)
  - paths must be non-empty (an empty allow-list is pointless and would
    produce an unreachable Ingress)
*/}}
{{- define "terrapod.validateWebhookIngress" -}}
{{- if .Values.webhookIngress.enabled -}}
{{- if not .Values.webhookIngress.hostname -}}
{{- fail "webhookIngress.enabled is true but webhookIngress.hostname is empty. Set the public hostname VCS webhooks (and any run-task callbacks) will reach Terrapod at." -}}
{{- end -}}
{{- if not .Values.web.enabled -}}
{{- fail "webhookIngress.enabled is true but web.enabled is false. The webhook Ingress routes to the web frontend — set web.enabled=true." -}}
{{- end -}}
{{- if not .Values.webhookIngress.paths -}}
{{- fail "webhookIngress.enabled is true but webhookIngress.paths is empty. The default allow-list ships in values.yaml; an empty list would produce an Ingress that accepts nothing." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the public webhook URL (no trailing slash, with scheme). Used by
configmap-api.yaml to default `api.config.public_webhook_url` when the
operator hasn't set it explicitly. Returns empty when no webhook Ingress
is configured — the API server then falls back to `external_url` for
webhook URL generation.
*/}}
{{- define "terrapod.publicWebhookURL" -}}
{{- if and .Values.webhookIngress.enabled .Values.webhookIngress.hostname -}}
{{- $scheme := ternary "https" "http" .Values.webhookIngress.tls -}}
{{- printf "%s://%s" $scheme .Values.webhookIngress.hostname -}}
{{- end -}}
{{- end -}}

{{/*
Validate the optional internal Ingress:
  - hostname is required when enabled (the internal route resolves to it)
  - web.enabled is required (this Ingress routes to the web BFF, same as
    the management Ingress)
  - paths must be non-empty (default is "/")
*/}}
{{- define "terrapod.validateInternalIngress" -}}
{{- if .Values.internalIngress.enabled -}}
{{- if not .Values.internalIngress.hostname -}}
{{- fail "internalIngress.enabled is true but internalIngress.hostname is empty. Set the internal hostname that listener pods and runner Jobs will use to reach Terrapod." -}}
{{- end -}}
{{- if not .Values.web.enabled -}}
{{- fail "internalIngress.enabled is true but web.enabled is false. The internal Ingress routes to the web frontend — set web.enabled=true." -}}
{{- end -}}
{{- if not .Values.internalIngress.paths -}}
{{- fail "internalIngress.enabled is true but internalIngress.paths is empty. The default (paths: [\"/\"]) ships in values.yaml; an empty list would produce an Ingress that accepts nothing." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the internal API URL (no trailing slash, with scheme). Returns
empty when the internal Ingress isn't configured. Operators wire this
into listener.apiUrl when they want listeners + runners to use the
internal route while users + CLI traffic continue to enter through the
primary `ingress`.
*/}}
{{- define "terrapod.internalAPIURL" -}}
{{- if and .Values.internalIngress.enabled .Values.internalIngress.hostname -}}
{{- $scheme := ternary "https" "http" .Values.internalIngress.tls -}}
{{- printf "%s://%s" $scheme .Values.internalIngress.hostname -}}
{{- end -}}
{{- end -}}
