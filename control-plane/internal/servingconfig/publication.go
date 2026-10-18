// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Identifies frontend publication protocols and the views retained during rollout.

package servingconfig

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
)

const ConditionRoutingReady = "RoutingReady"

// Serving configuration protocol selected with the frontend application.
const (
	ModelRoutingProtocol   uint32 = 5
	CurrentProtocol               = ModelRoutingProtocol
	CacheIsolationProtocol uint32 = 4
	AdmissionProtocol      uint32 = 3
	ProtocolAnnotation            = "inference.foretoken.io/serving-config-protocol"
)

// ConfigMapName keeps each application's immutable Pod volume on its supported protocol.
func ConfigMapName(frontend *inferencev1alpha1.FrontendService, version uint32) string {
	name := frontend.Name + "-serving"
	if len(name) > 63 {
		name = workload.ChildName(name, string(frontend.UID))
	}
	if version > AdmissionProtocol {
		name += fmt.Sprintf("-v%d", version)
	}
	return name
}

// ProtocolVersions lists the selected protocol first and older views retained during rollout.
// Publication, membership watches, and drain share the same ConfigMap identity range.
func ProtocolVersions(frontend *inferencev1alpha1.FrontendService) []uint32 {
	versions := []uint32{frontend.Status.ServingConfigVersion}
	for version := frontend.Status.ServingConfigVersion; version > AdmissionProtocol; version-- {
		versions = append(versions, version-1)
	}
	return versions
}

// PodConfigMapName reads the Pod's actual serving mount independently of protocol annotations.
func PodConfigMapName(pod *corev1.Pod) string {
	for _, volume := range pod.Spec.Volumes {
		if volume.Name == "serving" && volume.ConfigMap != nil {
			return volume.ConfigMap.Name
		}
	}
	return ""
}

// ContainsRouteTarget reports whether any execution topology still publishes the target being drained.
func ContainsRouteTarget(snapshot Snapshot, routeTargetID string) bool {
	for _, group := range snapshot.Groups {
		if group.RouteTargetID == routeTargetID {
			return true
		}
	}
	for _, component := range snapshot.PDComponents {
		if component.RouteTargetID == routeTargetID {
			return true
		}
	}
	for _, component := range snapshot.EPDComponents {
		if component.RouteTargetID == routeTargetID {
			return true
		}
	}
	return false
}

const SnapshotKey = "serving.json"
