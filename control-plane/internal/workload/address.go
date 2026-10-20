// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Derives stable workload names, ingress selectors, and internal endpoints.

package workload

import (
	"crypto/sha256"
	"fmt"
	"net"
	"strconv"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	kubevalidation "k8s.io/apimachinery/pkg/util/validation"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const frontendHTTPPortName = "http"

// ChildName derives a bounded child-resource name while preserving the complete ownership identity.
func ChildName(prefix, identity string) string {
	suffix := fmt.Sprintf("-%x", sha256.Sum256([]byte(identity)))[:11]
	if len(prefix)+len(suffix) <= 63 {
		return prefix + suffix
	}
	return prefix[:63-len(suffix)] + suffix
}

// ModelGroupServiceName returns the stable ingress Service name shared by workload and routing consumers.
func ModelGroupServiceName(group *inferencev1alpha1.ModelGroup) string {
	if len(kubevalidation.IsDNS1035Label(group.Name)) == 0 {
		return group.Name
	}

	prefix := dns1035NamePart(group.Name)
	if prefix == "" {
		prefix = "group"
	}
	identity := strings.ReplaceAll(dns1035NamePart(string(group.UID)), "-", "")
	if identity == "" {
		identity = "group"
	}
	const serviceNamePrefix = "mg-"
	prefixLimit := kubevalidation.DNS1035LabelMaxLength - len(serviceNamePrefix) - len(identity) - 1
	if len(prefix) > prefixLimit {
		prefix = strings.TrimRight(prefix[:prefixLimit], "-")
	}
	return serviceNamePrefix + prefix + "-" + identity
}

// ModelGroupServiceSelector selects the sole HTTP ingress, excluding distributed workers.
func ModelGroupServiceSelector(group *inferencev1alpha1.ModelGroup) map[string]string {
	selector := ModelGroupLabels(group)
	if group.Spec.NodeCount > 1 {
		selector[lwsv1.WorkerIndexLabelKey] = "0"
	}
	return selector
}

// FrontendPodEndpoint resolves the named HTTP listener used to observe one frontend replica.
func FrontendPodEndpoint(pod *corev1.Pod) (string, error) {
	return PodEndpoint(pod, frontendHTTPPortName)
}

// PodEndpoint resolves a declared HTTP listener for controllers observing the actual Pod runtime.
func PodEndpoint(pod *corev1.Pod, portName string) (string, error) {
	if pod.Status.PodIP == "" {
		return "", fmt.Errorf("Pod %q has no Pod IP", pod.Name)
	}
	for _, container := range pod.Spec.Containers {
		for _, port := range container.Ports {
			if port.Name == portName {
				address := net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(port.ContainerPort)))
				return "http://" + address, nil
			}
		}
	}
	return "", fmt.Errorf("Pod %q has no %q port", pod.Name, portName)
}

// ModelGroupLeaderWorkerSetName reserves a separate headless Service name for LWS
// and leaves room for worker ordinals and ControllerRevision label suffixes.
func ModelGroupLeaderWorkerSetName(group *inferencev1alpha1.ModelGroup) string {
	return "mg-" + string(group.UID)
}

// ModelGroupEndpoint returns the internal Service URL used for Group requests and lifecycle observation.
func ModelGroupEndpoint(group *inferencev1alpha1.ModelGroup, port int32) string {
	return fmt.Sprintf("http://%s.%s.svc:%d", ModelGroupServiceName(group), group.Namespace, port)
}

func dns1035NamePart(value string) string {
	return strings.Trim(strings.Map(func(character rune) rune {
		if character >= 'a' && character <= 'z' || character >= '0' && character <= '9' {
			return character
		}
		return '-'
	}, strings.ToLower(value)), "-")
}
