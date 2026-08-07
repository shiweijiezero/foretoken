// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Builds Kubernetes resources owned by one FrontendService.

package controllers

import (
	"fmt"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

func frontendServingConfigMapName(frontend *inferencev1alpha1.FrontendService) string {
	name := frontend.Name + "-serving"
	if len(name) <= 63 {
		return name
	}
	return kvChildName(name, string(frontend.UID))
}

func frontendDesiredResources(frontend *inferencev1alpha1.FrontendService, profile FrontendRuntimeProfile) (*appsv1.Deployment, *corev1.Service, *gatewayv1.HTTPRoute, error) {
	requests, limits, err := frontendResources(frontend.Spec.Resources)
	if err != nil {
		return nil, nil, nil, err
	}
	requestTimeout, err := gatewayDuration(frontend.Spec.Timeouts.Request)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse frontend request timeout: %w", err)
	}
	streamIdleSeconds, err := durationSeconds(frontend.Spec.Timeouts.StreamIdle)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse frontend stream idle timeout: %w", err)
	}
	labels := map[string]string{frontendServiceLabel: frontend.Name}
	replicas := int32(1)
	if frontend.Spec.Replicas != nil {
		replicas = *frontend.Spec.Replicas
	}
	automountToken := false
	enableServiceLinks := true
	runAsNonRoot := true
	fileSystemGroup := int64(65532)
	allowPrivilegeEscalation := false
	readOnlyRootFilesystem := true
	servingConfigMap := frontendServingConfigMapName(frontend)
	routerAlgorithm := frontend.Spec.RouterAlgorithm
	if routerAlgorithm == "" {
		routerAlgorithm = inferencev1alpha1.RouterAlgorithmKVAware
	}

	deployment := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken: &automountToken,
					EnableServiceLinks:           &enableServiceLinks,
					Volumes: []corev1.Volume{
						{
							Name: "serving",
							VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
								LocalObjectReference: corev1.LocalObjectReference{Name: servingConfigMap},
							}},
						},
						{Name: "tokenizer-cache", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
						{Name: "kv-indexer", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: kvIndexerSecretName, Items: []corev1.KeyToPath{{Key: kvIndexerSecretKey, Path: "key"}}}}},
					},
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   &runAsNonRoot,
						FSGroup:        &fileSystemGroup,
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:            "frontend",
						Image:           profile.Image,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: profile.Port, Protocol: corev1.ProtocolTCP}},
						Env: []corev1.EnvVar{
							{Name: "FORETOKEN_LISTEN_ADDRESS", Value: fmt.Sprintf("0.0.0.0:%d", profile.Port)},
							{Name: "FORETOKEN_SERVING_SNAPSHOT", Value: "/etc/foretoken/serving/serving.json"},
							{Name: "HF_HOME", Value: "/var/cache/foretoken/huggingface"},
							{Name: "FORETOKEN_STREAM_IDLE_SECONDS", Value: strconv.FormatInt(streamIdleSeconds, 10)},
							{Name: "FORETOKEN_KV_INDEX_KEY_PATH", Value: kvIndexerKeyPath},
							{Name: "FORETOKEN_ROUTER_ALGORITHM", Value: string(routerAlgorithm)},
						},
						VolumeMounts: []corev1.VolumeMount{
							{Name: "serving", MountPath: "/etc/foretoken/serving", ReadOnly: true},
							{Name: "tokenizer-cache", MountPath: "/var/cache/foretoken"},
							{Name: "kv-indexer", MountPath: "/etc/foretoken/kv-indexer", ReadOnly: true},
						},
						Resources: corev1.ResourceRequirements{Requests: requests, Limits: limits},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: &allowPrivilegeEscalation,
							ReadOnlyRootFilesystem:   &readOnlyRootFilesystem,
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
						},
						LivenessProbe:  frontendHTTPProbe("/healthz", 10),
						ReadinessProbe: frontendHTTPProbe("/readyz", 5),
					}},
				},
			},
		},
	}
	service := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: labels,
			Ports:    []corev1.ServicePort{{Name: "http", Port: profile.Port, TargetPort: intstr.FromString("http"), Protocol: corev1.ProtocolTCP}},
		},
	}
	return deployment, service, desiredHTTPRoute(frontend, profile, requestTimeout), nil
}

func frontendResources(resources inferencev1alpha1.FrontendResources) (corev1.ResourceList, corev1.ResourceList, error) {
	cpu, err := resource.ParseQuantity(string(resources.Requests.CPU))
	if err != nil {
		return nil, nil, fmt.Errorf("parse frontend CPU request: %w", err)
	}
	memory, err := resource.ParseQuantity(string(resources.Requests.Memory))
	if err != nil {
		return nil, nil, fmt.Errorf("parse frontend memory request: %w", err)
	}
	requests := corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory}
	limits := corev1.ResourceList{}
	if resources.Limits == nil {
		return requests, limits, nil
	}
	if resources.Limits.CPU != nil {
		quantity, err := resource.ParseQuantity(string(*resources.Limits.CPU))
		if err != nil {
			return nil, nil, fmt.Errorf("parse frontend CPU limit: %w", err)
		}
		limits[corev1.ResourceCPU] = quantity
	}
	if resources.Limits.Memory != nil {
		quantity, err := resource.ParseQuantity(string(*resources.Limits.Memory))
		if err != nil {
			return nil, nil, fmt.Errorf("parse frontend memory limit: %w", err)
		}
		limits[corev1.ResourceMemory] = quantity
	}
	return requests, limits, nil
}

func desiredHTTPRoute(frontend *inferencev1alpha1.FrontendService, profile FrontendRuntimeProfile, requestTimeout gatewayv1.Duration) *gatewayv1.HTTPRoute {
	gatewayGroup := gatewayv1.Group(gatewayv1.GroupName)
	gatewayKind := gatewayv1.Kind("Gateway")
	pathType := gatewayv1.PathMatchPathPrefix
	path := "/"
	parent := gatewayv1.ParentReference{Group: &gatewayGroup, Kind: &gatewayKind, Name: gatewayv1.ObjectName(profile.Gateway.Name)}
	if profile.Gateway.Namespace != "" && profile.Gateway.Namespace != frontend.Namespace {
		namespace := gatewayv1.Namespace(profile.Gateway.Namespace)
		parent.Namespace = &namespace
	}
	if profile.Gateway.SectionName != "" {
		sectionName := gatewayv1.SectionName(profile.Gateway.SectionName)
		parent.SectionName = &sectionName
	}
	port := gatewayv1.PortNumber(profile.Port)
	serviceKind := gatewayv1.Kind("Service")
	return &gatewayv1.HTTPRoute{
		TypeMeta:   metav1.TypeMeta{APIVersion: gatewayv1.GroupVersion.String(), Kind: "HTTPRoute"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: map[string]string{frontendServiceLabel: frontend.Name}},
		Spec: gatewayv1.HTTPRouteSpec{
			CommonRouteSpec: gatewayv1.CommonRouteSpec{ParentRefs: []gatewayv1.ParentReference{parent}},
			Hostnames:       []gatewayv1.Hostname{gatewayv1.Hostname(frontend.Spec.Hostname)},
			Rules: []gatewayv1.HTTPRouteRule{{
				Matches:  []gatewayv1.HTTPRouteMatch{{Path: &gatewayv1.HTTPPathMatch{Type: &pathType, Value: &path}}},
				Timeouts: &gatewayv1.HTTPRouteTimeouts{Request: &requestTimeout},
				BackendRefs: []gatewayv1.HTTPBackendRef{{BackendRef: gatewayv1.BackendRef{BackendObjectReference: gatewayv1.BackendObjectReference{
					Kind: &serviceKind,
					Name: gatewayv1.ObjectName(frontend.Name),
					Port: &port,
				}}}},
			}},
		},
	}
}

func frontendHTTPProbe(path string, periodSeconds int32) *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: path, Port: intstr.FromString("http")}},
		PeriodSeconds:    periodSeconds,
		TimeoutSeconds:   1,
		SuccessThreshold: 1,
		FailureThreshold: 3,
	}
}

func gatewayDuration(input inferencev1alpha1.Duration) (gatewayv1.Duration, error) {
	seconds, err := durationSeconds(input)
	if err != nil {
		return "", err
	}
	return gatewayv1.Duration(fmt.Sprintf("%ds", seconds)), nil
}

func durationSeconds(input inferencev1alpha1.Duration) (int64, error) {
	duration, err := time.ParseDuration(string(input))
	if err != nil {
		return 0, err
	}
	if duration <= 0 || duration%time.Second != 0 {
		return 0, fmt.Errorf("duration must be a positive whole number of seconds")
	}
	return int64(duration / time.Second), nil
}

func frontendDeploymentAvailable(deployment *appsv1.Deployment) bool {
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas == 0 {
		return false
	}
	conditionAvailable := false
	for _, condition := range deployment.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			conditionAvailable = true
			break
		}
	}
	return deployment.Status.ObservedGeneration >= deployment.Generation &&
		deployment.Status.AvailableReplicas == *deployment.Spec.Replicas && conditionAvailable
}

func httpRouteAccepted(route *gatewayv1.HTTPRoute, parent GatewayParent, routeNamespace string) bool {
	for _, status := range route.Status.Parents {
		if !parentReferenceMatches(status.ParentRef, parent, routeNamespace) {
			continue
		}
		accepted := meta.FindStatusCondition(status.Conditions, string(gatewayv1.RouteConditionAccepted))
		resolved := meta.FindStatusCondition(status.Conditions, string(gatewayv1.RouteConditionResolvedRefs))
		if accepted != nil && resolved != nil && accepted.ObservedGeneration == route.Generation && resolved.ObservedGeneration == route.Generation && accepted.Status == metav1.ConditionTrue && resolved.Status == metav1.ConditionTrue {
			return true
		}
	}
	return false
}

func parentReferenceMatches(actual gatewayv1.ParentReference, expected GatewayParent, routeNamespace string) bool {
	if string(actual.Name) != expected.Name {
		return false
	}
	expectedNamespace := expected.Namespace
	if expectedNamespace == "" {
		expectedNamespace = routeNamespace
	}
	actualNamespace := routeNamespace
	if actual.Namespace != nil {
		actualNamespace = string(*actual.Namespace)
	}
	if actualNamespace != expectedNamespace {
		return false
	}
	return expected.SectionName == "" || actual.SectionName != nil && string(*actual.SectionName) == expected.SectionName
}
