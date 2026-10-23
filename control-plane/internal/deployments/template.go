// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the Chart-compiled controller and directory contracts selected by deployments.
package deployments

import (
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
)

// TemplateKey is the installed platform's compiled instance-controller configuration.
const TemplateKey = "deployment-template.json"

// AccessKey supplies namespace-local public access settings to instance CLI callers.
const AccessKey = "deployment-access.json"

// Directory describes administrator-authorized storage independently of an instance's cache selection.
// Existing claims remain borrowed; other entries are materialized once per Kubernetes namespace.
type Directory struct {
	Directory      string                     `json:"directory"`
	ExistingClaim  bool                       `json:"existingClaim,omitempty"`
	Server         string                     `json:"server,omitempty"`
	Path           string                     `json:"path,omitempty"`
	HostPath       string                     `json:"hostPath,omitempty"`
	Hostnames      []string                   `json:"hostnames,omitempty"`
	Capacity       string                     `json:"capacity,omitempty"`
	DirectoryOwner string                     `json:"directoryOwner,omitempty"`
	MountOptions   []string                   `json:"mountOptions,omitempty"`
	NodeAffinity   *corev1.VolumeNodeAffinity `json:"nodeAffinity,omitempty"`
}

// Template is rendered by the installed chart and selected atomically for each deployment request.
// It reuses the chart's workload and permissions rather than maintaining a parallel Go installer.
type Template struct {
	Controller     appsv1.Deployment    `json:"controller"`
	Configuration  corev1.ConfigMap     `json:"configuration"`
	Rules          []rbacv1.PolicyRule  `json:"rules"`
	MetricsService *corev1.Service      `json:"metricsService,omitempty"`
	Directories    map[string]Directory `json:"directories"`
}
