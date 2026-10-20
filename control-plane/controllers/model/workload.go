// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Builds and reconciles model workload resources from resolved execution contracts.

package model

import (
	"context"
	"fmt"
	"maps"
	"math"
	"path"
	"slices"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllmomni"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// desiredDeployment builds the isolated model-server workload from a resolved ModelGroup contract.
func desiredDeployment(group *inferencev1alpha1.ModelGroup, imagePullSecrets []corev1.LocalObjectReference, files runtimeconfig.ApplicationFiles) (*appsv1.Deployment, error) {
	var launchJSON, launchEnv, command string
	var startupSeconds, drainSeconds int64
	switch group.Spec.Runtime.Backend {
	case "vllm":
		launchPlan, err := vllmconfig.BuildLaunchPlan(group.Spec)
		if err != nil {
			return nil, fmt.Errorf("build vLLM launch plan: %w", err)
		}
		launchJSON, err = launchPlan.JSON()
		if err != nil {
			return nil, fmt.Errorf("marshal vLLM launch plan: %w", err)
		}
		launchEnv, command = "FORETOKEN_VLLM_LAUNCH_PLAN", "foretoken-model-server"
		startupSeconds, drainSeconds = launchPlan.Lifecycle.StartupSeconds, launchPlan.Lifecycle.DrainSeconds
	case vllmomniconfig.Backend:
		launchPlan, err := vllmomniconfig.BuildLaunchPlan(group.Spec)
		if err != nil {
			return nil, fmt.Errorf("build vLLM-Omni launch plan: %w", err)
		}
		launchJSON, err = launchPlan.JSON()
		if err != nil {
			return nil, fmt.Errorf("marshal vLLM-Omni launch plan: %w", err)
		}
		launchEnv, command = "FORETOKEN_OMNI_LAUNCH_PLAN", "foretoken-omni-model-server"
		startupSeconds, drainSeconds = launchPlan.Lifecycle.StartupSeconds, launchPlan.Lifecycle.DrainSeconds
	default:
		return nil, fmt.Errorf("unsupported inference backend %q", group.Spec.Runtime.Backend)
	}
	labels := workload.ModelGroupLabels(group)
	var annotations map[string]string
	if group.Spec.Network != "" {
		annotations = map[string]string{multusNetworksAnnotation: group.Spec.Network}
	}
	if group.Spec.Runtime.SourceRevision != "" {
		if annotations == nil {
			annotations = make(map[string]string)
		}
		annotations[runtimeconfig.SourceRevisionAnnotation] = group.Spec.Runtime.SourceRevision
	}
	replicas := int32(1)
	revisionHistoryLimit := int32(10)
	progressDeadlineSeconds := int32(startupSeconds)
	automountToken := false
	enableServiceLinks := true
	allowPrivilegeEscalation := false
	capabilities := &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}
	if group.Spec.RDMA != nil {
		// RDMA completion queues pin userspace memory. IPC_LOCK permits that
		// without making the model container privileged.
		capabilities.Add = []corev1.Capability{"IPC_LOCK"}
	}
	// vLLM managed-engine reserves at least five seconds for process-group shutdown.
	terminationGracePeriodSeconds := drainSeconds + 5
	startupFailureThreshold := int32(math.Ceil(float64(startupSeconds) / 10))
	ports := []corev1.ContainerPort{{Name: "model-server", ContainerPort: group.Spec.Runtime.Port, Protocol: corev1.ProtocolTCP}}
	env := []corev1.EnvVar{
		{Name: launchEnv, Value: launchJSON},
		{Name: "FORETOKEN_INTERNAL_LISTEN", Value: fmt.Sprintf("0.0.0.0:%d", group.Spec.Runtime.Port)},
		{Name: "FORETOKEN_MODEL_GROUP_UID", Value: string(group.UID)},
		{Name: "FORETOKEN_POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
	}
	env = append(env, runtimeconfig.RedisConnectionEnv(group.Spec.Runtime.AdmissionStore, runtimeconfig.AdmissionStoreURLEnv)...)
	if group.Spec.Runtime.InstanceAdmissionProtocol == 0 && group.Spec.Runtime.InstanceAdmission != nil {
		env = append(env, corev1.EnvVar{Name: "FORETOKEN_MAX_ACCEPTED_REQUESTS", Value: strconv.FormatUint(uint64(group.Spec.Runtime.InstanceAdmission.MaxConcurrentRequests), 10)})
	}
	if group.Spec.NodeCount > 1 {
		env = append(env, corev1.EnvVar{Name: "FORETOKEN_MEMBER_IP", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}})
	}
	if group.Spec.Runtime.Backend == "vllm" {
		env = append(env,
			corev1.EnvVar{Name: "FORETOKEN_KV_INDEX_KEY_PATH", Value: servingconfig.KVIndexerKeyPath},
			corev1.EnvVar{Name: "FORETOKEN_KV_SCOPE_ID", Value: servingconfig.KVScopeID(group)},
		)
	}
	if group.Spec.Runtime.PreparationVersion > 0 {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.ModelPreparationScopeEnv, Value: group.Spec.ModelPoolRef.UID + "/" + group.Spec.Revision})
	}
	if groupApplicationURL(group, files) != "" {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.SourceDirectoryEnv, Value: files.Directory()})
	}
	env = append(env, vllmconfig.RuntimeCacheEnv(group.Spec.Artifacts.Cache, group.Namespace, group.Labels[inferencev1alpha1.DeploymentUIDLabel], group.Spec.Runtime.TritonCacheDirectory, group.Spec.Runtime.PreparationVersion)...)
	env = append(env, runtimeconfig.HuggingFaceEnv(group.Spec.Artifacts.HuggingFaceAccess)...)
	env = append(env, vllmconfig.PDEnvironment(group.Spec.PDRuntime)...)
	if group.Spec.PDRuntime != nil && group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		ports = append(ports, corev1.ContainerPort{Name: "mc-bootstrap", ContainerPort: group.Spec.PDRuntime.BootstrapPort, Protocol: corev1.ProtocolTCP})
	}

	requests := corev1.ResourceList{
		corev1.ResourceCPU:    resource.MustParse(string(group.Spec.Resources.Requests.CPU)),
		corev1.ResourceMemory: resource.MustParse(string(group.Spec.Resources.Requests.Memory)),
	}
	limits := corev1.ResourceList{}
	if group.Spec.Resources.Limits != nil {
		if group.Spec.Resources.Limits.CPU != nil {
			limits[corev1.ResourceCPU] = resource.MustParse(string(*group.Spec.Resources.Limits.CPU))
		}
		if group.Spec.Resources.Limits.Memory != nil {
			limits[corev1.ResourceMemory] = resource.MustParse(string(*group.Spec.Resources.Limits.Memory))
		}
	}
	deviceResource := corev1.ResourceName(group.Spec.Accelerator.DeviceResourceName)
	deviceCount := *resource.NewQuantity(int64(group.Spec.Resources.Requests.GPU.Count), resource.DecimalSI)
	requests[deviceResource] = deviceCount
	limits[deviceResource] = deviceCount
	if rdma := group.Spec.RDMA; rdma != nil {
		rdmaResource := corev1.ResourceName(rdma.ResourceName)
		rdmaCount := *resource.NewQuantity(int64(rdma.ResourceCount), resource.DecimalSI)
		requests[rdmaResource] = rdmaCount
		limits[rdmaResource] = rdmaCount
	}
	volumes := []corev1.Volume{
		{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		{Name: "dshm", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory}}},
	}
	mounts := []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}, {Name: "dshm", MountPath: "/dev/shm"}}
	if groupUsesLiveInstanceAdmission(group) {
		volumes = append(volumes, corev1.Volume{Name: instanceAdmissionVolumeName, VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: instanceAdmissionConfigMapName(group.Spec.ModelPoolRef.UID)}}}})
		mounts = append(mounts, corev1.VolumeMount{Name: instanceAdmissionVolumeName, MountPath: instanceAdmissionDirectory, ReadOnly: true})
		env = append(env, corev1.EnvVar{Name: instanceAdmissionConfigEnv, Value: instanceAdmissionDirectory + "/" + instanceAdmissionKey})
	}
	if group.Spec.Runtime.Backend == "vllm" {
		volumes = append(volumes, corev1.Volume{Name: "kv-indexer", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: servingconfig.KVIndexerSecretName, Items: []corev1.KeyToPath{{Key: servingconfig.KVIndexerSecretKey, Path: "key"}}}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "kv-indexer", MountPath: "/etc/foretoken/kv-indexer", ReadOnly: true})
	}
	if cache := group.Spec.Artifacts.Cache; cache != nil {
		volumes = append(volumes, corev1.Volume{Name: runtimeconfig.CacheVolumeName, VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: cache.ClaimName}}})
		mounts = append(mounts, corev1.VolumeMount{Name: runtimeconfig.CacheVolumeName, MountPath: cache.MountPath})
		if group.Spec.Runtime.Backend == "vllm" {
			ports = append(ports, corev1.ContainerPort{Name: "cache-observe", ContainerPort: runtimeconfig.CacheObservationPort(group.Spec.Runtime.Port), Protocol: corev1.ProtocolTCP})
			env = append(env, runtimeconfig.CacheObserverEnv(*cache, group.Spec.Runtime.Port)...)
			if group.Spec.Runtime.PreparationVersion >= runtimeconfig.SharedModelCachePreparationVersion {
				env = append(env, corev1.EnvVar{Name: runtimeconfig.ProfileRootEnv, Value: path.Join(runtimeconfig.DeploymentCacheDirectory(cache.MountPath, group.Labels[inferencev1alpha1.DeploymentUIDLabel]), "profiles")})
			}
		} else {
			env = append(env,
				corev1.EnvVar{Name: "FORETOKEN_CACHE_MOUNT_PATH", Value: cache.MountPath},
				corev1.EnvVar{Name: runtimeconfig.CacheClaimEnv, Value: cache.ClaimName},
			)
		}
	}
	if ec := group.Spec.ECRuntime; ec != nil {
		directory := runtimeconfig.EncoderCacheDirectory(ec)
		volumes = append(volumes, corev1.Volume{Name: "ec-shared-storage", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: ec.SharedStorageClaim}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "ec-shared-storage", MountPath: path.Join(ec.SharedStoragePath, directory), SubPath: directory})
	}
	kvVolumes, kvMounts, kvEnv := vllmconfig.KVStorage(group.Spec.KVRuntime)
	volumes = append(volumes, kvVolumes...)
	mounts = append(mounts, kvMounts...)
	env = append(env, kvEnv...)
	var runtimeClassName *string
	if group.Spec.Accelerator.RuntimeClassName != "" {
		runtimeClassName = &group.Spec.Accelerator.RuntimeClassName
	}

	readinessPath := "/readyz"
	if groupUsesLiveInstanceAdmission(group) {
		readinessPath = instanceAdmissionReadyPath
	}
	deployment := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas:                &replicas,
			RevisionHistoryLimit:    &revisionHistoryLimit,
			ProgressDeadlineSeconds: &progressDeadlineSeconds,
			Strategy:                appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector:                &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: maps.Clone(labels), Annotations: annotations},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken:  &automountToken,
					EnableServiceLinks:            &enableServiceLinks,
					ImagePullSecrets:              slices.Clone(imagePullSecrets),
					RestartPolicy:                 corev1.RestartPolicyAlways,
					DNSPolicy:                     corev1.DNSClusterFirst,
					SchedulerName:                 corev1.DefaultSchedulerName,
					RuntimeClassName:              runtimeClassName,
					NodeSelector:                  maps.Clone(group.Spec.Accelerator.NodeSelector),
					Tolerations:                   workload.AcceleratorTolerations(group.Spec.Accelerator.DeviceResourceName),
					TerminationGracePeriodSeconds: &terminationGracePeriodSeconds,
					Volumes:                       volumes,
					Containers: []corev1.Container{{
						Name:            "model-server",
						Image:           group.Spec.Runtime.Image,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Command:         []string{command},
						Args:            []string{},
						Ports:           ports,
						Env:             env,
						VolumeMounts:    mounts,
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: &allowPrivilegeEscalation,
							Capabilities:             capabilities,
						},
						StartupProbe:             modelServerProbe("/readyz", 10, startupFailureThreshold),
						LivenessProbe:            modelServerProbe("/healthz", 10, 3),
						ReadinessProbe:           modelServerProbe(readinessPath, 5, 3),
						TerminationMessagePath:   corev1.TerminationMessagePathDefault,
						TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
						Resources:                corev1.ResourceRequirements{Requests: requests, Limits: limits},
					}},
				},
			},
		},
	}
	files.Configure(&deployment.Spec.Template, &deployment.Spec.Template.Spec.Containers[0], groupApplicationURL(group, files), command)
	mountPreparationSource(group, &deployment.Spec.Template)
	if err := vllmconfig.ConfigureWeightSources(group.Spec, &deployment.Spec.Template); err != nil {
		return nil, err
	}
	container := &deployment.Spec.Template.Spec.Containers[0]
	if err := configureModelEnvironment(group.Spec, container); err != nil {
		return nil, err
	}
	access := runtimeconfig.WorkloadCacheAccess(&deployment.Spec.Template, group.Spec.Artifacts.Cache, group.Spec.Runtime.PreparationVersion)
	accesses := []runtimeconfig.VolumeAccess{access}
	if ec := group.Spec.ECRuntime; ec != nil {
		identity := runtimeconfig.DefaultStorageIdentity()
		if ec.SharedStorageIdentity != nil {
			identity = *ec.SharedStorageIdentity
		}
		accesses = append(accesses, runtimeconfig.VolumeAccess{
			Name: "ec-shared-storage", Identity: identity,
			Directories: []string{runtimeconfig.EncoderCacheDirectory(ec)},
		})
	}
	runtimeconfig.ConfigurePodStorage(&deployment.Spec.Template.Spec, files.Image, access.Identity, accesses...)
	return deployment, nil
}

// groupApplicationURL reads the immutable selection, including pre-selection source cohorts.
func groupApplicationURL(group *inferencev1alpha1.ModelGroup, files runtimeconfig.ApplicationFiles) string {
	if group.Spec.Runtime.ApplicationURL != "" {
		return group.Spec.Runtime.ApplicationURL
	}
	return files.Ref("model-server", group.Spec.Runtime.SourceRevision)
}

// reconcileDeployment applies the ModelGroup Deployment and returns its persisted state.
func (reconciler *GroupReconciler) reconcileDeployment(ctx context.Context, group *inferencev1alpha1.ModelGroup, desired *appsv1.Deployment) (*appsv1.Deployment, error) {
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return nil, fmt.Errorf("set Deployment owner: %w", err)
	}
	current := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
		if !metav1.IsControlledBy(current, group) {
			return nil, fmt.Errorf("Deployment %q is not controlled by ModelGroup", current.Name)
		}
		if group.Spec.Runtime.PreparationVersion == 0 {
			return current, nil
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("get Deployment: %w", err)
	}
	if err := runtimeconfig.PlaceRuntimeCache(ctx, reconciler.Client, group.Namespace, group.Spec.Artifacts.Cache, &desired.Spec.Template); err != nil {
		return nil, err
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
		return nil, fmt.Errorf("apply Deployment: %w", err)
	}
	return desired, nil
}

// configureModelEnvironment appends ordered model variables after platform configuration.
// Platform-owned values must remain consistent with the launch plan and child processes.
func configureModelEnvironment(spec inferencev1alpha1.ModelGroupSpec, container *corev1.Container) error {
	managed := make(map[string]bool, len(container.Env))
	for _, variable := range container.Env {
		managed[variable.Name] = true
	}
	// The device plugin supplies allocation variables after Pod template generation.
	if strings.HasPrefix(spec.Accelerator.DeviceResourceName, "nvidia.com/") {
		managed["NVIDIA_VISIBLE_DEVICES"] = true
	}
	if spec.NodeCount > 1 {
		managed["LWS_WORKER_INDEX"] = true
		managed["LWS_LEADER_ADDRESS"] = true
	}
	for _, variable := range spec.Runtime.Env {
		if managed[variable.Name] || strings.HasPrefix(variable.Name, "FORETOKEN_") || vllmconfig.ManagesEnvironment(spec, variable.Name) {
			return fmt.Errorf("env name %q is managed by Foretoken", variable.Name)
		}
		container.Env = append(container.Env, *variable.DeepCopy())
	}
	return nil
}

// reconcileService applies the stable Service owned by the ModelGroup.
func (reconciler *GroupReconciler) reconcileService(ctx context.Context, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) error {
	labels := workload.ModelGroupLabels(group)
	selector := workload.ModelGroupServiceSelector(group)
	// Scrape identity belongs on the Service, independently of workload selectors.
	// Annotations preserve full resource names beyond the label-value length limit.
	serviceLabels := maps.Clone(labels)
	serviceLabels[workload.ModelGroupUIDLabel] = string(group.UID)
	desired := &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name: workload.ModelGroupServiceName(group), Namespace: group.Namespace, Labels: serviceLabels,
			Annotations: map[string]string{
				"inference.foretoken.io/model-service":             pool.Spec.ModelServiceRef.Name,
				"inference.foretoken.io/served-model":              group.Spec.Artifacts.Model,
				"inference.foretoken.io/model-pool":                pool.Spec.PoolName,
				"inference.foretoken.io/model-instance":            strconv.Itoa(int(group.Spec.Ordinal)),
				"inference.foretoken.io/model-instance-created-at": group.CreationTimestamp.UTC().Format(time.RFC3339),
			},
		},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: selector,
			Ports: []corev1.ServicePort{{
				Name:       "model-server",
				Port:       group.Spec.Runtime.Port,
				TargetPort: intstr.FromString("model-server"),
				Protocol:   corev1.ProtocolTCP,
			}},
		},
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill && group.Spec.PDRuntime != nil {
		desired.Spec.Ports = append(desired.Spec.Ports, corev1.ServicePort{
			Name:       "mc-bootstrap",
			Port:       group.Spec.PDRuntime.BootstrapPort,
			TargetPort: intstr.FromString("mc-bootstrap"),
			Protocol:   corev1.ProtocolTCP,
		})
	}
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return fmt.Errorf("set Service owner: %w", err)
	}
	current := new(corev1.Service)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
		if !metav1.IsControlledBy(current, group) {
			return fmt.Errorf("Service %q is not controlled by ModelGroup", current.Name)
		}
	} else if !apierrors.IsNotFound(err) {
		return fmt.Errorf("get Service: %w", err)
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
		return fmt.Errorf("apply Service: %w", err)
	}
	return nil
}

func modelServerProbe(path string, periodSeconds, failureThreshold int32) *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: path, Port: intstr.FromString("model-server")}},
		PeriodSeconds:    periodSeconds,
		TimeoutSeconds:   1,
		SuccessThreshold: 1,
		FailureThreshold: failureThreshold,
	}
}

// reconcileNetworkPolicy permits deployment-local traffic, runtime peers, and trusted metrics scrapers to reach a ModelGroup.
func (reconciler *GroupReconciler) reconcileNetworkPolicy(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	deploymentUID := group.Labels[inferencev1alpha1.DeploymentUIDLabel]
	if deploymentUID == "" {
		return fmt.Errorf("ModelGroup %q has no deployment UID", group.Name)
	}
	labels := workload.ModelGroupLabels(group)
	deploymentLabels := map[string]string{inferencev1alpha1.DeploymentUIDLabel: deploymentUID}
	frontendSelector := &metav1.LabelSelector{
		MatchLabels: deploymentLabels,
		MatchExpressions: []metav1.LabelSelectorRequirement{{
			Key: workload.FrontendServiceLabel, Operator: metav1.LabelSelectorOpExists,
		}},
	}
	controllerLabels := maps.Clone(deploymentLabels)
	controllerLabels[workload.ControlPlanePodLabel] = workload.ControlPlanePodLabelValue
	controllerSelector := &metav1.LabelSelector{MatchLabels: controllerLabels}
	protocol := corev1.ProtocolTCP
	modelServerPort := intstr.FromString("model-server")
	if reconciler.ControlPlaneNamespace == "" {
		return fmt.Errorf("control-plane namespace is not configured")
	}
	ingress := []networkingv1.NetworkPolicyIngressRule{{
		From: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: frontendSelector,
			},
			{
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": reconciler.ControlPlaneNamespace}},
				PodSelector:       controllerSelector,
			},
		},
		Ports: []networkingv1.NetworkPolicyPort{{Protocol: &protocol, Port: &modelServerPort}},
	}}
	var preparationIngress []networkingv1.NetworkPolicyIngressRule
	if group.Spec.Artifacts.Cache != nil {
		cacheObservationPort := intstr.FromString("cache-observe")
		preparationIngress = []networkingv1.NetworkPolicyIngressRule{{
			From: []networkingv1.NetworkPolicyPeer{{
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": reconciler.ControlPlaneNamespace}},
				PodSelector:       controllerSelector,
			}},
			Ports: []networkingv1.NetworkPolicyPort{{Protocol: &protocol, Port: &cacheObservationPort}},
		}}
		ingress = append(ingress, preparationIngress...)
	}
	if group.Spec.NodeCount > 1 {
		// EngineCore and collectives allocate additional ports during the group handshake.
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: labels},
		}}})
	}
	ingress[0].From = append(ingress[0].From, networkingv1.NetworkPolicyPeer{
		NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
			metricsScraperNamespaceLabel: metricsScraperNamespaceValue,
		}},
	})
	if group.Spec.PDRuntime != nil {
		// Mooncake opens bidirectional runtime side channels on dynamic ports
		// after bootstrap. Restrict them to the same controller-owned P/D linked processing unit.
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
				workload.ModelGroupPDPipelineScopeLabel: group.Spec.PDRuntime.ServiceUID,
			}},
		}}})
		if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
			bootstrapPort := intstr.FromInt32(group.Spec.PDRuntime.BootstrapPort)
			ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{
				From:  []networkingv1.NetworkPolicyPeer{{PodSelector: frontendSelector}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &protocol, Port: &bootstrapPort}},
			})
		}
	}
	ingress = append(ingress, vllmconfig.WeightSourceIngress(group.Spec)...)
	desired := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: networkingv1.SchemeGroupVersion.String(), Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace, Labels: labels},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: labels},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress:     ingress,
		},
	}
	policies := []*networkingv1.NetworkPolicy{desired}
	if group.Spec.Artifacts.Source != inferencev1alpha1.ModelSourceLocal {
		policies = append(policies, &networkingv1.NetworkPolicy{
			TypeMeta:   metav1.TypeMeta{APIVersion: networkingv1.SchemeGroupVersion.String(), Kind: "NetworkPolicy"},
			ObjectMeta: metav1.ObjectMeta{Name: "prepare-" + string(group.UID), Namespace: group.Namespace},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{workload.ModelPreparationGroupLabel: group.Name}},
				PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
				Ingress:     preparationIngress,
			},
		})
	}
	for _, desired := range policies {
		if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set NetworkPolicy owner: %w", err)
		}
		current := new(networkingv1.NetworkPolicy)
		if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
			if !metav1.IsControlledBy(current, group) {
				return fmt.Errorf("NetworkPolicy %q is not controlled by ModelGroup", current.Name)
			}
		} else if !apierrors.IsNotFound(err) {
			return fmt.Errorf("get NetworkPolicy: %w", err)
		}
		if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
			return fmt.Errorf("apply NetworkPolicy: %w", err)
		}
	}
	return nil
}
