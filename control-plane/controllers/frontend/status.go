// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes frontend availability and current-target completion from observed lifecycle state.
package frontend

import (
	"context"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	frontendConditionMaterialized = "WorkloadMaterialized"
	frontendConditionAvailable    = "WorkloadAvailable"
	frontendConditionRouteReady   = "RouteAccepted"
)

type frontendState struct {
	// ExecutionReady concerns the selected application and target capacity; Available
	// still reports a previous Deployment that is serving during deferred replacement.
	ExecutionReady bool
	Stopped        bool
	Materialized   bool
	CachePending   bool
	Available      bool
	RouteRequired  bool
	RouteReady     bool
	Configuration  metav1.Condition
	FailureReason  string
	FailureMessage string
}

// updateStatus publishes workload, route, and serving-snapshot readiness for the frontend.
func (reconciler *Reconciler) updateStatus(ctx context.Context, frontend *inferencev1alpha1.FrontendService, state frontendState) error {
	base := frontend.DeepCopy()
	frontend.Status.ObservedGeneration = frontend.Generation
	if state.FailureReason != "" {
		for _, conditionType := range []string{
			frontendConditionMaterialized,
			frontendConditionAvailable,
			frontendConditionRouteReady,
			servingconfig.ConditionRoutingReady,
			workload.ConditionReady,
		} {
			setFrontendCondition(frontend, conditionType, metav1.ConditionFalse, state.FailureReason, state.FailureMessage)
		}
	} else if state.Stopped {
		for _, conditionType := range []string{frontendConditionMaterialized, frontendConditionRouteReady} {
			setFrontendCondition(frontend, conditionType, metav1.ConditionUnknown, "NotReconciled", "No frontend replicas are requested; retained resources are unchanged")
		}
		for _, conditionType := range []string{frontendConditionAvailable, servingconfig.ConditionRoutingReady, workload.ConditionReady} {
			setFrontendCondition(frontend, conditionType, metav1.ConditionFalse, "ScaledToZero", "All frontend replicas have stopped")
		}
	} else {
		reason, message := "NotMaterialized", "Frontend resources were not materialized"
		if state.Materialized {
			reason, message = "Applied", "Frontend resources were applied"
		}
		setFrontendCondition(frontend, frontendConditionMaterialized, workload.ConditionStatus(state.Materialized), reason, message)
		reason, message = "Unavailable", "The frontend Deployment is not available"
		if state.Available {
			reason, message = "Available", "The frontend Deployment is available"
		}
		setFrontendCondition(frontend, frontendConditionAvailable, workload.ConditionStatus(state.Available), reason, message)
		if state.RouteRequired {
			reason, message := "Pending", "The HTTPRoute is not accepted and resolved"
			if state.RouteReady {
				reason, message = "Accepted", "The HTTPRoute is accepted and resolved"
			}
			setFrontendCondition(frontend, frontendConditionRouteReady, workload.ConditionStatus(state.RouteReady), reason, message)
		} else {
			setFrontendCondition(frontend, frontendConditionRouteReady, metav1.ConditionTrue, "NotRequired", "Local mode exposes the frontend through its LoadBalancer Service")
		}
		if state.Configuration.Status == metav1.ConditionTrue {
			frontend.Status.AppliedServingSnapshotVersion = frontend.Status.ServingSnapshotVersion
		}
		setFrontendCondition(
			frontend,
			servingconfig.ConditionRoutingReady,
			state.Configuration.Status,
			state.Configuration.Reason,
			state.Configuration.Message,
		)
		reason, message = frontendReadyFailure(state)
		setFrontendCondition(frontend, workload.ConditionReady, workload.ConditionStatus(reason == "Ready"), reason, message)
	}
	target := frontendTargetReadiness(state)
	setFrontendCondition(frontend, workload.ConditionTargetReady, target.Status, target.Reason, target.Message)
	if reflect.DeepEqual(base.Status, frontend.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, frontend, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update FrontendService status: %w", err)
	}
	return nil
}

func setFrontendCondition(frontend *inferencev1alpha1.FrontendService, conditionType string, status metav1.ConditionStatus, reason, message string) {
	meta.SetStatusCondition(&frontend.Status.Conditions, metav1.Condition{Type: conditionType, Status: status, Reason: reason, Message: message, ObservedGeneration: frontend.Generation})
}

func frontendReadyFailure(state frontendState) (string, string) {
	switch {
	case state.CachePending:
		return "WaitingForCache", "Waiting for model workloads to prepare the shared runtime cache"
	case !state.Materialized:
		return "NotMaterialized", "Frontend resources are not materialized"
	case !state.Available:
		return "WorkloadUnavailable", "The frontend Deployment is not available"
	case !state.ExecutionReady:
		return "ExecutionPending", "The selected frontend application and target replicas are not ready"
	case state.RouteRequired && !state.RouteReady:
		return "RouteNotAccepted", "The HTTPRoute is not accepted and resolved by its Gateway"
	case state.Configuration.Status != metav1.ConditionTrue:
		return state.Configuration.Reason, state.Configuration.Message
	default:
		return "Ready", "The frontend workload and backend routing are ready"
	}
}

// frontendTargetReadiness separates rejected intent from recoverable workload and API waits.
func frontendTargetReadiness(state frontendState) metav1.Condition {
	if state.Stopped {
		return metav1.Condition{Status: metav1.ConditionTrue, Reason: "ScaledToZero", Message: "All frontend replicas have stopped"}
	}
	reason, message := frontendReadyFailure(state)
	if state.FailureReason != "" {
		reason, message = state.FailureReason, state.FailureMessage
	}
	switch state.Configuration.Reason {
	case "ConfigurationRejected", "BackendDeploymentRequired", "FrontendDeploymentRequired":
		reason, message = state.Configuration.Reason, state.Configuration.Message
	}
	status := metav1.ConditionUnknown
	switch reason {
	case "Ready":
		status = metav1.ConditionTrue
	case "InvalidIntent", "RuntimeProfileIncomplete", "ConfigurationRejected", "BackendDeploymentRequired", "FrontendDeploymentRequired":
		status = metav1.ConditionFalse
	}
	return metav1.Condition{Status: status, Reason: reason, Message: message}
}
