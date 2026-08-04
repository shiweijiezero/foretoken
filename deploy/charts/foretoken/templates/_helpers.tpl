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
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end }}
