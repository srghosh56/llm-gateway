{{- define "llm-gateway.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "llm-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "llm-gateway.labels" -}}
{{ include "llm-gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Name of the Secret holding GATEWAY_ADMIN_API_KEY, GATEWAY_DATABASE_URL, POSTGRES_PASSWORD. */}}
{{- define "llm-gateway.secretName" -}}
{{- default (printf "%s-secrets" (include "llm-gateway.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* Restricted-profile security context shared by every container in the chart. */}}
{{- define "llm-gateway.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "llm-gateway.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .uid }}
fsGroup: {{ .uid }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Env shared by the gateway Deployment and the migration Job. */}}
{{- define "llm-gateway.env" -}}
- name: GATEWAY_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "llm-gateway.secretName" . }}
      key: GATEWAY_DATABASE_URL
- name: GATEWAY_ADMIN_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "llm-gateway.secretName" . }}
      key: GATEWAY_ADMIN_API_KEY
- name: GATEWAY_OLLAMA_BASE_URL
  value: {{ .Values.gateway.ollamaBaseUrl | default (printf "http://%s-ollama:11434/v1" (include "llm-gateway.fullname" .)) | quote }}
{{- range $name, $value := .Values.gateway.config }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}
