{{- define "wiki-memory.fullname" -}}{{ .Release.Name }}-wiki-memory{{- end }}
{{- define "wiki-memory.image" -}}
{{- if .Values.image.digest -}}{{ .Values.image.repository }}@{{ .Values.image.digest }}{{- else -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}
{{- end }}
{{- define "wiki-memory.secretName" -}}
{{- default (include "wiki-memory.fullname" .) .Values.secrets.existingSecret -}}
{{- end }}
