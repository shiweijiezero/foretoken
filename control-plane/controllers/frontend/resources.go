// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Builds the frontend Deployment, Service, and optional Gateway route.

package frontend

import (
	"fmt"
	"maps"
	"path"
	"slices"
	"strconv"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

// Derive the frontend workload, stable Service, and optional gateway route as one desired-state
// unit so ports, timeouts, labels, and ownership cannot drift across independently built objects.
func frontendDesiredResources(frontend *inferencev1alpha1.FrontendService, profile FrontendRuntimeProfile) (*appsv1.Deployment, *corev1.Service, *gatewayv1.HTTPRoute, error) {
	requests, limits, err := frontendResources(frontend.Spec.Resources)
	if err != nil {
		return nil, nil, nil, err
	}
	requestTimeoutSeconds, err := runtimeconfig.WholeDurationSeconds(frontend.Spec.Timeouts.Request)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse frontend request timeout: %w", err)
	}
	requestTimeout := gatewayv1.Duration(fmt.Sprintf("%ds", requestTimeoutSeconds))
	drainSeconds, err := runtimeconfig.WholeDurationSeconds(frontend.Spec.Timeouts.Drain)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse frontend drain timeout: %w", err)
	}
	labels := map[string]string{workload.FrontendServiceLabel: frontend.Name}
	replicas := int32(1)
	if frontend.Spec.Replicas != nil {
		replicas = *frontend.Spec.Replicas
	}
	automountToken := false
	enableServiceLinks := true
	allowPrivilegeEscalation := false
	readOnlyRootFilesystem := true
	terminationGracePeriodSeconds := drainSeconds + 5
	servingConfigMap := servingconfig.ConfigMapName(frontend, frontend.Status.ServingConfigVersion)
	if frontend.Spec.RouterPipeline.Filter.Algorithm == "" ||
		frontend.Spec.RouterPipeline.Scorer.Algorithm == "" ||
		frontend.Spec.RouterPipeline.Picker.Algorithm == "" {
		return nil, nil, nil, fmt.Errorf("frontend routerPipeline was not defaulted")
	}
	cacheMountPath := "/var/cache/foretoken"
	if profile.RuntimeCache != nil {
		cacheMountPath = profile.RuntimeCache.MountPath
	}
	modelRoot := runtimeconfig.ModelDirectory(cacheMountPath)
	modelCache := runtimeconfig.ModelCacheDirectory(cacheMountPath)
	deploymentUID := frontend.Labels[inferencev1alpha1.DeploymentUIDLabel]
	runtimeRoot := runtimeconfig.DeploymentCacheDirectory(cacheMountPath, deploymentUID)
	var application *inferencev1alpha1.ApplicationSelection
	if frontend.Status.Application != nil {
		application = &frontend.Status.Application.ApplicationSelection
	}
	preparationVersion := runtimeconfig.ModelPreparationVersion(application, "")
	sharedCache := preparationVersion >= runtimeconfig.SharedModelCachePreparationVersion
	huggingFaceHome := modelRoot
	if sharedCache {
		huggingFaceHome = path.Join(runtimeRoot, "huggingface")
	}
	frontendEnv := []corev1.EnvVar{
		{Name: "FORETOKEN_LISTEN_ADDRESS", Value: fmt.Sprintf("0.0.0.0:%d", profile.Port)},
		{Name: "FORETOKEN_SERVING_SNAPSHOT", Value: "/etc/foretoken/serving/serving.json"},
		{Name: "FORETOKEN_FRONTEND_UID", Value: string(frontend.UID)},
		{Name: runtimeconfig.DeploymentUIDEnv, Value: deploymentUID},
		{Name: "FORETOKEN_POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
		{Name: "HF_HOME", Value: huggingFaceHome},
		{Name: runtimeconfig.ModelRootEnv, Value: modelRoot},
		{Name: "FORETOKEN_DRAIN_SECONDS", Value: strconv.FormatInt(drainSeconds, 10)},
		{Name: "FORETOKEN_KV_INDEX_KEY_PATH", Value: servingconfig.KVIndexerKeyPath},
	}
	if sharedCache {
		frontendEnv = append(frontendEnv,
			corev1.EnvVar{Name: "HOME", Value: runtimeRoot},
			corev1.EnvVar{Name: "XDG_CACHE_HOME", Value: path.Join(runtimeRoot, ".cache")},
			corev1.EnvVar{Name: "XDG_CONFIG_HOME", Value: path.Join(runtimeRoot, ".config")},
			corev1.EnvVar{Name: "HF_HUB_CACHE", Value: path.Join(huggingFaceHome, "hub")},
			corev1.EnvVar{Name: runtimeconfig.ModelCacheRootEnv, Value: modelCache},
			corev1.EnvVar{Name: runtimeconfig.PreparationRootEnv, Value: path.Join(runtimeRoot, "prepared")},
		)
	}
	annotations := map[string]string{servingconfig.ProtocolAnnotation: fmt.Sprint(frontend.Status.ServingConfigVersion)}
	if profile.ApplicationURL != "" {
		frontendEnv = append(frontendEnv, corev1.EnvVar{Name: runtimeconfig.SourceDirectoryEnv, Value: profile.ApplicationFiles.Directory()})
	}
	if profile.SourceRevision != "" {
		annotations[runtimeconfig.SourceRevisionAnnotation] = profile.SourceRevision
	}
	frontendEnv = append(frontendEnv, runtimeconfig.RedisConnectionEnv(profile.AdmissionStore, runtimeconfig.AdmissionStoreURLEnv)...)
	frontendEnv = append(frontendEnv, runtimeconfig.ResponseStoreEnv(profile.ResponseStore, frontend.Namespace+"/"+frontend.Name)...)
	frontendEnv = append(frontendEnv, runtimeconfig.HuggingFaceEnv(profile.HuggingFaceAccess)...)
	cacheVolume := corev1.Volume{Name: "runtime-cache", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}
	if profile.RuntimeCache != nil {
		frontendEnv = append(frontendEnv,
			corev1.EnvVar{Name: runtimeconfig.RuntimeCacheBindingEnv, Value: runtimeconfig.RuntimeCacheIdentity(frontend.Namespace, profile.RuntimeCache.ClaimName, preparationVersion)},
			corev1.EnvVar{Name: "FORETOKEN_CACHE_MOUNT_PATH", Value: cacheMountPath},
			corev1.EnvVar{Name: runtimeconfig.TemporaryModelRootEnv, Value: runtimeconfig.ModelDirectory("/tmp/foretoken-runtime-cache")},
		)
		cacheVolume.VolumeSource = corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: profile.RuntimeCache.ClaimName}}
	}
	volumes := []corev1.Volume{
		{Name: "request-temporary", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		{Name: "serving", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: servingConfigMap}}}},
		cacheVolume,
		{Name: "kv-indexer", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: servingconfig.KVIndexerSecretName, Items: []corev1.KeyToPath{{Key: servingconfig.KVIndexerSecretKey, Path: "key"}}}}},
	}
	mounts := []corev1.VolumeMount{
		{Name: "request-temporary", MountPath: "/tmp"},
		{Name: "serving", MountPath: "/etc/foretoken/serving", ReadOnly: true},
		{Name: "runtime-cache", MountPath: cacheMountPath},
		{Name: "kv-indexer", MountPath: "/etc/foretoken/kv-indexer", ReadOnly: true},
	}
	if profile.RuntimeCache != nil {
		volumes = append(volumes, corev1.Volume{Name: "runtime-cache-temporary", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "runtime-cache-temporary", MountPath: "/tmp/foretoken-runtime-cache"})
	}

	serviceAccountName := ""
	if storage := frontend.Spec.VideoTasks; storage != nil {
		if storage.ClaimName == "" || storage.RetentionSeconds <= 0 || profile.WorkerImage == "" {
			return nil, nil, nil, fmt.Errorf("videoTasks requires a claimName, positive retentionSeconds and platform worker image")
		}
		automountToken = true
		serviceAccountName = frontend.Name
		frontendEnv = append(frontendEnv,
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_OUTPUT_MOUNT", Value: runtimeconfig.VideoTaskOutputMount},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_WORKER_IMAGE", Value: profile.WorkerImage},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_ENDPOINT", Value: fmt.Sprintf("http://%s.%s.svc:%d", frontend.Name, frontend.Namespace, profile.Port)},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_OUTPUT_CLAIM", Value: storage.ClaimName},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_FRONTEND_UID", Value: string(frontend.UID)},
		)
		volumes = append(volumes, corev1.Volume{Name: "video-task-storage", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: storage.ClaimName}}})
		mounts = append(mounts,
			corev1.VolumeMount{Name: "video-task-storage", MountPath: runtimeconfig.VideoTaskOutputMount + "/tasks", SubPath: "tasks"},
			corev1.VolumeMount{Name: "video-task-storage", MountPath: runtimeconfig.VideoTaskOutputMount + "/inputs", SubPath: "inputs", ReadOnly: true},
		)
	}

	deployment := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: maps.Clone(labels), Annotations: annotations},
				Spec: corev1.PodSpec{
					ServiceAccountName:            serviceAccountName,
					AutomountServiceAccountToken:  &automountToken,
					EnableServiceLinks:            &enableServiceLinks,
					ImagePullSecrets:              slices.Clone(profile.ImagePullSecrets),
					TerminationGracePeriodSeconds: &terminationGracePeriodSeconds,
					Volumes:                       volumes,
					Containers: []corev1.Container{{
						Name:            "frontend",
						Image:           profile.Image,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: profile.Port, Protocol: corev1.ProtocolTCP}},
						Env:             frontendEnv,
						VolumeMounts:    mounts,
						Resources:       corev1.ResourceRequirements{Requests: requests, Limits: limits},
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
	profile.ApplicationFiles.Configure(&deployment.Spec.Template, &deployment.Spec.Template.Spec.Containers[0], profile.ApplicationURL, "foretoken-frontend")
	cacheAccess := runtimeconfig.WorkloadCacheAccess(&deployment.Spec.Template, profile.RuntimeCache, preparationVersion)
	accesses := []runtimeconfig.VolumeAccess{cacheAccess}
	if frontend.Spec.VideoTasks != nil {
		if profile.VideoStorageIdentity == nil {
			return nil, nil, nil, fmt.Errorf("video storage identity was not resolved")
		}
		accesses = append(accesses, runtimeconfig.VolumeAccess{
			Name: "video-task-storage", Identity: *profile.VideoStorageIdentity,
			Directories: []string{"tasks", "inputs"},
		})
	}
	runtimeconfig.ConfigurePodStorage(&deployment.Spec.Template.Spec, profile.ApplicationFiles.Image, cacheAccess.Identity, accesses...)
	serviceType := corev1.ServiceTypeClusterIP
	if profile.Gateway == nil {
		serviceType = corev1.ServiceTypeLoadBalancer
	}
	service := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: labels},
		Spec: corev1.ServiceSpec{
			Type:     serviceType,
			Selector: labels,
			Ports:    []corev1.ServicePort{{Name: "http", Port: profile.Port, TargetPort: intstr.FromString("http"), Protocol: corev1.ProtocolTCP}},
		},
	}
	if profile.Gateway == nil {
		return deployment, service, nil, nil
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

// desiredHTTPRoute exposes public API paths under the explicit host or the frontend's own Service identity.
func desiredHTTPRoute(frontend *inferencev1alpha1.FrontendService, profile FrontendRuntimeProfile, requestTimeout gatewayv1.Duration) *gatewayv1.HTTPRoute {
	hostname := frontend.Spec.Hostname
	if hostname == "" {
		hostname = fmt.Sprintf("%s.%s.svc", frontend.Name, frontend.Namespace)
	}
	gatewayGroup := gatewayv1.Group(gatewayv1.GroupName)
	gatewayKind := gatewayv1.Kind("Gateway")
	pathType := gatewayv1.PathMatchPathPrefix
	publicPaths := []string{"/v1", "/tokenize", "/detokenize"}
	matches := make([]gatewayv1.HTTPRouteMatch, 0, len(publicPaths))
	for index := range publicPaths {
		matches = append(matches, gatewayv1.HTTPRouteMatch{Path: &gatewayv1.HTTPPathMatch{Type: &pathType, Value: &publicPaths[index]}})
	}
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
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: map[string]string{workload.FrontendServiceLabel: frontend.Name}},
		Spec: gatewayv1.HTTPRouteSpec{
			CommonRouteSpec: gatewayv1.CommonRouteSpec{ParentRefs: []gatewayv1.ParentReference{parent}},
			Hostnames:       []gatewayv1.Hostname{gatewayv1.Hostname(hostname)},
			Rules: []gatewayv1.HTTPRouteRule{{
				Matches:  matches,
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

// desiredVideoTaskAccess builds the namespace permissions delegated to video-enabled frontend replicas.
func desiredVideoTaskAccess(frontend *inferencev1alpha1.FrontendService) []client.Object {
	metadata := metav1.ObjectMeta{Name: frontend.Name, Namespace: frontend.Namespace, Labels: map[string]string{workload.FrontendServiceLabel: frontend.Name}}
	return []client.Object{
		&corev1.ServiceAccount{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"}, ObjectMeta: metadata},
		&rbacv1.Role{TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"}, ObjectMeta: metadata,
			Rules: []rbacv1.PolicyRule{{APIGroups: []string{"inference.foretoken.io"}, Resources: []string{"videotasks"}, Verbs: []string{"get", "create", "patch", "delete"}}}},
		&rbacv1.RoleBinding{TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"}, ObjectMeta: metadata,
			RoleRef:  rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: frontend.Name},
			Subjects: []rbacv1.Subject{{Kind: "ServiceAccount", Name: frontend.Name, Namespace: frontend.Namespace}}},
	}
}
