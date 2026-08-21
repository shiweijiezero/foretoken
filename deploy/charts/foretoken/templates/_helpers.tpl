{{/* SPDX-License-Identifier: Apache-2.0 */}}
{{/* SPDX-FileCopyrightText: Copyright contributors to the Foretoken project */}}

{{/* Defines shared naming, labeling, and image-rendering helpers. */}}

{{- define "foretoken.compactName" -}}
{{- if gt (len .) 63 -}}
{{- printf "%s-%s" (. | trunc 54 | trimSuffix "-") (. | sha256sum | trunc 8) -}}
{{- else -}}
{{- . -}}
{{- end -}}
{{- end }}

{{- define "foretoken.fullname" -}}
{{- include "foretoken.compactName" (printf "%s-control-plane" .Release.Name) -}}
{{- end }}

{{- define "foretoken.clusterName" -}}
{{- include "foretoken.compactName" (printf "%s-%s-control-plane" .Release.Namespace .Release.Name) -}}
{{- end }}

{{- define "foretoken.frontendGatewayClassName" -}}
{{- include "foretoken.compactName" (printf "%s-%s-gateway-class" .Release.Namespace .Release.Name) -}}
{{- end }}

{{- define "foretoken.frontendGatewayName" -}}
{{- if .Values.frontend.gateway.create -}}
{{- include "foretoken.compactName" (printf "%s-gateway" .Release.Name) -}}
{{- else -}}
{{- .Values.frontend.gateway.name -}}
{{- end -}}
{{- end }}

{{- define "foretoken.frontendGatewayNamespace" -}}
{{- if .Values.frontend.gateway.create -}}
{{- .Release.Namespace -}}
{{- else -}}
{{- .Values.frontend.gateway.namespace -}}
{{- end -}}
{{- end }}

{{- define "foretoken.frontendGatewaySectionName" -}}
{{- if .Values.frontend.gateway.create -}}
{{- "http" -}}
{{- else -}}
{{- .Values.frontend.gateway.sectionName -}}
{{- end -}}
{{- end }}

{{- define "foretoken.selectorLabels" -}}
app.kubernetes.io/name: foretoken-control-plane
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "foretoken.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "foretoken.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{- define "foretoken.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end }}

{{- define "foretoken.validateValues" -}}
{{- if eq (trim .Values.image.repository) "" -}}
{{- fail "image.repository must reference a control-plane image" -}}
{{- end -}}
{{- if .Values.frontend.enabled -}}
{{- if eq (trim .Values.frontend.image) "" -}}
{{- fail "frontend.image is required when frontend.enabled=true" -}}
{{- end -}}
{{- if and .Values.frontend.gateway.create (ne .Values.frontend.mode "gateway") -}}
{{- fail "frontend.gateway.create requires frontend.mode=gateway" -}}
{{- end -}}
{{- if eq .Values.frontend.mode "gateway" -}}
{{- if .Values.frontend.gateway.create -}}
{{- if eq (trim .Values.frontend.gateway.controllerName) "" -}}
{{- fail "frontend.gateway.controllerName is required when creating a Gateway" -}}
{{- end -}}
{{- else -}}
{{- if eq (trim .Values.frontend.gateway.name) "" -}}
{{- fail "frontend.gateway.name is required when using an existing Gateway" -}}
{{- end -}}
{{- if eq (trim .Values.frontend.gateway.namespace) "" -}}
{{- fail "frontend.gateway.namespace is required when using an existing Gateway" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne (trim .Values.runtime.vllm.image) "" -}}
{{- if eq (trim .Values.runtime.vllm.gpu.resourceName) "" -}}
{{- fail "runtime.vllm.gpu.resourceName is required when runtime.vllm.image is set" -}}
{{- end -}}
{{- if ne (eq (trim .Values.runtime.vllm.gpu.nodeSelector.key) "") (eq (trim .Values.runtime.vllm.gpu.nodeSelector.value) "") -}}
{{- fail "runtime.vllm.gpu.nodeSelector.key and value must be set together" -}}
{{- end -}}
{{- end -}}
{{- end }}
