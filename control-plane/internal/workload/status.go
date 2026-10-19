// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Shares readiness and materialization condition names across workload lifecycle owners.

package workload

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

const (
	ConditionReady              = "Ready"
	ConditionTargetReady        = "TargetReady"
	ConditionGroupsMaterialized = "GroupsMaterialized"
)

// ConditionStatus converts an observed lifecycle predicate to a Kubernetes condition status.
func ConditionStatus(value bool) metav1.ConditionStatus {
	if value {
		return metav1.ConditionTrue
	}
	return metav1.ConditionFalse
}
