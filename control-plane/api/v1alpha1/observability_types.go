// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines workload-owned alert selection without coupling monitoring to serving readiness.

package v1alpha1

// FrontendObservability selects observations owned by one FrontendService.
type FrontendObservability struct {
	// +optional
	Alerts *FrontendAlerts `json:"alerts,omitempty"`
}

// FrontendAlerts selects only frontend-scoped rules; omitted rules remain disabled.
// +kubebuilder:validation:XValidation:rule="!has(self.rules) || !self.rules.exists(r, r == 'ForetokenAdmissionCapacityRejectionRatioHigh') || (has(self.thresholds) && has(self.thresholds.admission) && has(self.thresholds.admission.capacityRejectionRatio) && has(self.thresholds.admission.minResultRate))",message="the capacity rejection alert requires capacityRejectionRatio and minResultRate"
// +kubebuilder:validation:XValidation:rule="!has(self.rules) || !self.rules.exists(r, r == 'ForetokenAdmissionTimeoutRatioHigh') || (has(self.thresholds) && has(self.thresholds.admission) && has(self.thresholds.admission.timeoutRatio) && has(self.thresholds.admission.minResultRate))",message="the timeout alert requires timeoutRatio and minResultRate"
// +kubebuilder:validation:XValidation:rule="!has(self.rules) || !self.rules.exists(r, r == 'ForetokenAdmissionAdmittedQueueP95High') || (has(self.thresholds) && has(self.thresholds.admission) && has(self.thresholds.admission.admittedQueueP95Seconds) && has(self.thresholds.admission.minQueuedAdmissionRate))",message="the queue latency alert requires admittedQueueP95Seconds and minQueuedAdmissionRate"
type FrontendAlerts struct {
	// +optional
	// +listType=set
	// +kubebuilder:validation:MaxItems=6
	// +kubebuilder:validation:items:Enum=ForetokenMetricsTargetDown;ForetokenFrontendHTTPResponseStart5xxRatioHigh;ForetokenAdmissionCapacityRejectionRatioHigh;ForetokenAdmissionTimeoutRatioHigh;ForetokenAdmissionAdmittedQueueP95High;ForetokenAdmissionTelemetryMissing
	Rules []string `json:"rules,omitempty"`

	// +optional
	// +kubebuilder:default={}
	Thresholds *FrontendAlertThresholds `json:"thresholds,omitempty"`
}

// FrontendAlertThresholds configures selected frontend rules without enabling them.
type FrontendAlertThresholds struct {
	// +optional
	// +kubebuilder:default={}
	Admission *AdmissionAlertThresholds `json:"admission,omitempty"`
}

// AdmissionAlertThresholds sets HTTP admission alert tolerances for one service or each Pod.
// Business thresholds have no defaults; operation defaults are applied by API admission.
type AdmissionAlertThresholds struct {
	// +optional
	// +kubebuilder:default=service
	// +kubebuilder:validation:Enum=service;pod
	Scope string `json:"scope,omitempty"`

	// +optional
	// +kubebuilder:default="1m"
	// +kubebuilder:validation:Pattern="^([0-9]+(s|m|h))+$"
	Window Duration `json:"window,omitempty"`

	// +optional
	// +kubebuilder:default="5m"
	// +kubebuilder:validation:Pattern="^([0-9]+(s|m|h))+$"
	For Duration `json:"for,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=1
	CapacityRejectionRatio *float64 `json:"capacityRejectionRatio,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=1
	TimeoutRatio *float64 `json:"timeoutRatio,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:ExclusiveMinimum=true
	AdmittedQueueP95Seconds *float64 `json:"admittedQueueP95Seconds,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:ExclusiveMinimum=true
	MinResultRate *float64 `json:"minResultRate,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:ExclusiveMinimum=true
	MinQueuedAdmissionRate *float64 `json:"minQueuedAdmissionRate,omitempty"`
}

// ModelObservability selects observations owned by one ModelService.
type ModelObservability struct {
	// +optional
	Alerts *ModelAlerts `json:"alerts,omitempty"`
}

// ModelAlerts selects rules for a model's owned execution groups.
// +kubebuilder:validation:XValidation:rule="!has(self.rules) || !self.rules.exists(rule, rule == 'ForetokenNVIDIAGPUPowerUsageHigh') || (has(self.thresholds) && has(self.thresholds.nvidiaPowerWatts))",message="the power alert requires an explicit positive nvidiaPowerWatts threshold"
type ModelAlerts struct {
	// +optional
	// +listType=set
	// +kubebuilder:validation:MaxItems=3
	// +kubebuilder:validation:items:Enum=ForetokenMetricsTargetDown;ForetokenNVIDIAGPUTemperatureHigh;ForetokenNVIDIAGPUPowerUsageHigh
	Rules []string `json:"rules,omitempty"`

	// +optional
	// +kubebuilder:default={}
	Thresholds *ModelAlertThresholds `json:"thresholds,omitempty"`
}

// ModelAlertThresholds configures the selected model rules, not their activation.
type ModelAlertThresholds struct {
	// +optional
	// +kubebuilder:default=85
	// +kubebuilder:validation:Minimum=0
	NVIDIATemperatureCelsius *float64 `json:"nvidiaTemperatureCelsius,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:ExclusiveMinimum=true
	NVIDIAPowerWatts *float64 `json:"nvidiaPowerWatts,omitempty"`
}
