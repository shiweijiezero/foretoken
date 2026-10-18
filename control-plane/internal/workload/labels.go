// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Identifies model members, frontend replicas, and their network peers.

package workload

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

const FrontendServiceLabel = "inference.foretoken.io/frontend-service"

const (
	ModelGroupLabel                = "inference.foretoken.io/model-group"
	ModelPreparationGroupLabel     = "inference.foretoken.io/model-preparation-group"
	ModelGroupUIDLabel             = "inference.foretoken.io/model-group-uid"
	ModelGroupRoleLabel            = "inference.foretoken.io/model-role"
	ModelGroupPDPipelineScopeLabel = "inference.foretoken.io/pd-pipeline-scope"
	ControlPlanePodLabel           = "app.kubernetes.io/name"
	ControlPlanePodLabelValue      = "foretoken-control-plane"
)

// ModelGroupLabels returns the common member selectors used by Group workloads and their observers.
func ModelGroupLabels(group *inferencev1alpha1.ModelGroup) map[string]string {
	labels := map[string]string{ModelGroupLabel: group.Name, ModelGroupRoleLabel: string(group.Spec.Role)}
	if group.Spec.PDRuntime != nil {
		labels[ModelGroupPDPipelineScopeLabel] = group.Spec.PDRuntime.ServiceUID
	}
	return labels
}
