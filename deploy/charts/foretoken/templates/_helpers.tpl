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
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end }}

{{- define "foretoken.validateValues" -}}
{{- if eq (trim .Values.image.repository) "" -}}
{{- fail "image.repository must reference a control-plane image" -}}
{{- end -}}
{{- if and (eq (trim .Values.image.tag) "") (eq (trim .Values.image.digest) "") -}}
{{- fail "set image.tag or image.digest for the control-plane image" -}}
{{- end -}}
{{- if .Values.frontend.enabled -}}
{{- if eq (trim .Values.frontend.image) "" -}}
{{- fail "frontend.image is required when frontend.enabled=true" -}}
{{- end -}}
{{- if eq (trim .Values.frontend.gateway.name) "" -}}
{{- fail "frontend.gateway.name is required when frontend.enabled=true" -}}
{{- end -}}
{{- if eq (trim .Values.frontend.gateway.namespace) "" -}}
{{- fail "frontend.gateway.namespace is required when frontend.enabled=true" -}}
{{- end -}}
{{- end -}}
{{- if ne (trim .Values.runtime.vllm.image) "" -}}
{{- if eq (trim .Values.runtime.vllm.accelerator.type) "" -}}
{{- fail "runtime.vllm.accelerator.type is required when runtime.vllm.image is set" -}}
{{- end -}}
{{- if eq (trim .Values.runtime.vllm.accelerator.resourceName) "" -}}
{{- fail "runtime.vllm.accelerator.resourceName is required when runtime.vllm.image is set" -}}
{{- end -}}
{{- if eq (trim .Values.runtime.vllm.accelerator.nodeSelector.key) "" -}}
{{- fail "runtime.vllm.accelerator.nodeSelector.key is required when runtime.vllm.image is set" -}}
{{- end -}}
{{- if eq (trim .Values.runtime.vllm.accelerator.nodeSelector.value) "" -}}
{{- fail "runtime.vllm.accelerator.nodeSelector.value is required when runtime.vllm.image is set" -}}
{{- end -}}
{{- end -}}
{{- end }}
