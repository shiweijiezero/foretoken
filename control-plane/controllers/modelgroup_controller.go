// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles single-member ModelGroups into isolated model-server workloads.

package controllers

import (
	"context"
	"fmt"
	"maps"
	"math"
	"reflect"
	"slices"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	conditionWorkloadMaterialized  = "WorkloadMaterialized"
	conditionWorkloadAvailable     = "WorkloadAvailable"
	conditionSchedulingCapacity    = "SchedulingCapacity"
	modelGroupLabel                = "inference.foretoken.io/model-group"
	modelGroupRoleLabel            = "inference.foretoken.io/model-role"
	modelGroupPDPipelineScopeLabel = "inference.foretoken.io/pd-pipeline-scope"
	multusNetworksAnnotation       = "k8s.v1.cni.cncf.io/networks"
	modelGroupFieldOwner           = "foretoken-modelgroup-controller"
	controlPlanePodLabel           = "app.kubernetes.io/name"
	controlPlanePodLabelValue      = "foretoken-control-plane"
)

// ModelGroupReconciler owns the Kubernetes workload for one execution Group.
type ModelGroupReconciler struct {
	client.Client
	DrainClient           ModelGroupDrainClient
	Now                   func() time.Time
	ControlPlaneNamespace string
	ImagePullSecrets      []corev1.LocalObjectReference
}

// SetupWithManager registers the ModelGroup controller and its owned resources.
func (reconciler *ModelGroupReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelGroup{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelGroupsForPod)).
		Complete(reconciler)
}

// modelGroupsForPod requeues the Group selected by a Deployment Pod when its
// scheduler condition changes. Pods are owned by ReplicaSets, not ModelGroups.
func (reconciler *ModelGroupReconciler) modelGroupsForPod(_ context.Context, object client.Object) []reconcile.Request {
	groupName := object.GetLabels()[modelGroupLabel]
	if groupName == "" {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: object.GetNamespace(), Name: groupName}}}
}

// Reconcile materializes the workload and derives readiness from the model server.
func (reconciler *ModelGroupReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	group := new(inferencev1alpha1.ModelGroup)
	if err := reconciler.Get(ctx, request.NamespacedName, group); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !group.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, group)
	}
	if err := reconciler.validateModelPoolOwnership(ctx, group); err != nil {
		return ctrl.Result{}, err
	}
	if err := validateGroupProfile(group); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupFailureState(err))
	}
	if err := ensureKVIndexerSecret(ctx, reconciler.Client, group.Namespace); err != nil {
		return ctrl.Result{}, err
	}
	if !controllerutil.ContainsFinalizer(group, modelGroupDrainFinalizer) {
		base := group.DeepCopy()
		controllerutil.AddFinalizer(group, modelGroupDrainFinalizer)
		if err := reconciler.Patch(ctx, group, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add ModelGroup drain finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	deployment, err := reconciler.reconcileDeployment(ctx, group)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.reconcileService(ctx, group); err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.reconcileNetworkPolicy(ctx, group); err != nil {
		return ctrl.Result{}, err
	}
	scheduling, err := reconciler.schedulingCapacity(ctx, group)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.updateStatus(ctx, group, modelGroupMaterializedState(modelGroupDeploymentAvailable(deployment), scheduling)); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelGroupReconciler) validateModelPoolOwnership(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	pool := new(inferencev1alpha1.ModelPool)
	key := client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}
	if err := reconciler.Get(ctx, key, pool); err != nil {
		return fmt.Errorf("get owning ModelPool: %w", err)
	}
	if group.Spec.ModelPoolRef.UID != string(pool.UID) || !metav1.IsControlledBy(group, pool) {
		return fmt.Errorf("ModelGroup %q is not owned by its referenced ModelPool", group.Name)
	}
	return nil
}

// Workload reconciliation and desired resources.

func (reconciler *ModelGroupReconciler) reconcileDeployment(ctx context.Context, group *inferencev1alpha1.ModelGroup) (*appsv1.Deployment, error) {
	desired, err := desiredDeployment(group, reconciler.ImagePullSecrets)
	if err != nil {
		return nil, err
	}
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return nil, fmt.Errorf("set Deployment owner: %w", err)
	}
	current := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
		if !metav1.IsControlledBy(current, group) {
			return nil, fmt.Errorf("Deployment %q is not controlled by ModelGroup", current.Name)
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("get Deployment: %w", err)
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
		return nil, fmt.Errorf("apply Deployment: %w", err)
	}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
		return nil, fmt.Errorf("get applied Deployment: %w", err)
	}
	return current, nil
}

func modelGroupLabels(group *inferencev1alpha1.ModelGroup) map[string]string {
	labels := map[string]string{modelGroupLabel: group.Name, modelGroupRoleLabel: string(group.Spec.Role)}
	if group.Spec.PDRuntime != nil {
		labels[modelGroupPDPipelineScopeLabel] = pdPipelineScopeID(group)
	}
	return labels
}

func desiredDeployment(group *inferencev1alpha1.ModelGroup, imagePullSecrets []corev1.LocalObjectReference) (*appsv1.Deployment, error) {
	launchPlan, err := vllmconfig.BuildLaunchPlan(group.Spec)
	if err != nil {
		return nil, fmt.Errorf("build vLLM launch plan: %w", err)
	}
	launchJSON, err := launchPlan.JSON()
	if err != nil {
		return nil, fmt.Errorf("marshal vLLM launch plan: %w", err)
	}
	labels := modelGroupLabels(group)
	var annotations map[string]string
	if group.Spec.Network != "" {
		annotations = map[string]string{multusNetworksAnnotation: group.Spec.Network}
	}
	replicas := int32(1)
	revisionHistoryLimit := int32(10)
	progressDeadlineSeconds := int32(launchPlan.Lifecycle.StartupSeconds)
	automountToken := false
	enableServiceLinks := true
	allowPrivilegeEscalation := false
	capabilities := &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}
	if group.Spec.PDRuntime != nil {
		// RDMA completion queues pin userspace memory. IPC_LOCK permits that
		// without making the model container privileged.
		capabilities.Add = []corev1.Capability{"IPC_LOCK"}
	}
	// vLLM managed-engine reserves at least five seconds for process-group shutdown.
	terminationGracePeriodSeconds := launchPlan.Lifecycle.DrainSeconds + 5
	startupFailureThreshold := int32(math.Ceil(float64(launchPlan.Lifecycle.StartupSeconds) / 10))
	ports := []corev1.ContainerPort{{Name: "model-server", ContainerPort: group.Spec.Runtime.Port, Protocol: corev1.ProtocolTCP}}
	env := []corev1.EnvVar{
		{Name: "FORETOKEN_VLLM_LAUNCH_PLAN", Value: launchJSON},
		{Name: "FORETOKEN_INTERNAL_LISTEN", Value: fmt.Sprintf("0.0.0.0:%d", group.Spec.Runtime.Port)},
		{Name: "FORETOKEN_KV_INDEX_KEY_PATH", Value: kvIndexerKeyPath},
		{Name: "FORETOKEN_KV_SCOPE_ID", Value: kvScopeID(group)},
		{Name: "FORETOKEN_MODEL_GROUP_UID", Value: string(group.UID)},
	}
	if group.Spec.PDRuntime != nil {
		env = append(env,
			corev1.EnvVar{Name: "VLLM_MOONCAKE_BOOTSTRAP_PORT", Value: strconv.Itoa(int(group.Spec.PDRuntime.BootstrapPort))},
			corev1.EnvVar{Name: "VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT", Value: strconv.Itoa(int(group.Spec.PDRuntime.AbortRequestTimeoutSeconds))},
		)
		if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
			ports = append(ports, corev1.ContainerPort{Name: "mc-bootstrap", ContainerPort: group.Spec.PDRuntime.BootstrapPort, Protocol: corev1.ProtocolTCP})
		}
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
	if pd := group.Spec.PDRuntime; pd != nil {
		rdmaResource := corev1.ResourceName(pd.RDMAResourceName)
		rdmaCount := *resource.NewQuantity(int64(pd.RDMAResourceCount), resource.DecimalSI)
		requests[rdmaResource] = rdmaCount
		limits[rdmaResource] = rdmaCount
	}
	volumes := []corev1.Volume{
		{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		{Name: "dshm", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory}}},
		{Name: "kv-indexer", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: kvIndexerSecretName, Items: []corev1.KeyToPath{{Key: kvIndexerSecretKey, Path: "key"}}}}},
	}
	mounts := []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}, {Name: "dshm", MountPath: "/dev/shm"}, {Name: "kv-indexer", MountPath: "/etc/foretoken/kv-indexer", ReadOnly: true}}
	if group.Spec.ECRuntime != nil {
		volumes = append(volumes, corev1.Volume{Name: "ec-shared-storage", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: group.Spec.ECRuntime.SharedStorageClaim}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "ec-shared-storage", MountPath: group.Spec.ECRuntime.SharedStoragePath})
	}
	if group.Spec.KVRuntime != nil && group.Spec.KVRuntime.Offload != nil && group.Spec.KVRuntime.Offload.Filesystem {
		volumes = append(volumes, corev1.Volume{Name: "kv-offload", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "kv-offload", MountPath: vllmconfig.FilesystemOffloadMountPath})
	}
	if group.Spec.KVRuntime != nil && group.Spec.KVRuntime.MooncakeStore != nil {
		store := group.Spec.KVRuntime.MooncakeStore
		volumes = append(volumes, corev1.Volume{Name: "mooncake-store-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: store.ConfigMapName}, Items: []corev1.KeyToPath{{Key: store.ConfigMapKey, Path: "mooncake.json"}}}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "mooncake-store-config", MountPath: "/etc/foretoken/mooncake/mooncake.json", SubPath: "mooncake.json", ReadOnly: true})
		env = append(env, corev1.EnvVar{Name: "MOONCAKE_CONFIG_PATH", Value: "/etc/foretoken/mooncake/mooncake.json"}, corev1.EnvVar{Name: "PYTHONHASHSEED", Value: store.PythonHashSeed})
	}
	var runtimeClassName *string
	if group.Spec.Accelerator.RuntimeClassName != "" {
		runtimeClassName = &group.Spec.Accelerator.RuntimeClassName
	}

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas:                &replicas,
			RevisionHistoryLimit:    &revisionHistoryLimit,
			ProgressDeadlineSeconds: &progressDeadlineSeconds,
			Strategy:                appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector:                &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels, Annotations: annotations},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken:  &automountToken,
					EnableServiceLinks:            &enableServiceLinks,
					ImagePullSecrets:              slices.Clone(imagePullSecrets),
					RestartPolicy:                 corev1.RestartPolicyAlways,
					DNSPolicy:                     corev1.DNSClusterFirst,
					SchedulerName:                 corev1.DefaultSchedulerName,
					RuntimeClassName:              runtimeClassName,
					NodeSelector:                  maps.Clone(group.Spec.Accelerator.NodeSelector),
					TerminationGracePeriodSeconds: &terminationGracePeriodSeconds,
					SecurityContext: &corev1.PodSecurityContext{
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: volumes,
					Containers: []corev1.Container{{
						Name:            "model-server",
						Image:           group.Spec.Runtime.Image,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Command:         []string{"foretoken-model-server"},
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
						ReadinessProbe:           modelServerProbe("/readyz", 5, 3),
						TerminationMessagePath:   corev1.TerminationMessagePathDefault,
						TerminationMessagePolicy: corev1.TerminationMessageReadFile,
						Resources:                corev1.ResourceRequirements{Requests: requests, Limits: limits},
					}},
				},
			},
		},
	}, nil
}

func (reconciler *ModelGroupReconciler) reconcileService(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	labels := modelGroupLabels(group)
	desired := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace, Labels: labels},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: labels,
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

func (reconciler *ModelGroupReconciler) reconcileNetworkPolicy(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	labels := modelGroupLabels(group)
	protocol := corev1.ProtocolTCP
	modelServerPort := intstr.FromString("model-server")
	if reconciler.ControlPlaneNamespace == "" {
		return fmt.Errorf("control-plane namespace is not configured")
	}
	ingress := []networkingv1.NetworkPolicyIngressRule{{
		From: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
					Key:      frontendServiceLabel,
					Operator: metav1.LabelSelectorOpExists,
				}}},
			},
			{
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": reconciler.ControlPlaneNamespace}},
				PodSelector:       &metav1.LabelSelector{MatchLabels: map[string]string{controlPlanePodLabel: controlPlanePodLabelValue}},
			},
		},
		Ports: []networkingv1.NetworkPolicyPort{{Protocol: &protocol, Port: &modelServerPort}},
	}}
	if group.Spec.PDRuntime != nil {
		// Mooncake opens bidirectional runtime side channels on dynamic ports
		// after bootstrap. Restrict them to the same controller-owned P/D linked processing unit.
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
				modelGroupPDPipelineScopeLabel: pdPipelineScopeID(group),
			}},
		}}})
		if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
			bootstrapPort := intstr.FromInt32(group.Spec.PDRuntime.BootstrapPort)
			ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{
				From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
					Key:      frontendServiceLabel,
					Operator: metav1.LabelSelectorOpExists,
				}}}}},
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: &protocol, Port: &bootstrapPort}},
			})
		}
	}
	desired := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: networkingv1.SchemeGroupVersion.String(), Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace, Labels: labels},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: labels},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress:     ingress,
		},
	}
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
	return nil
}

// Scheduling and status projection.

type modelGroupConditionState struct {
	status  metav1.ConditionStatus
	reason  string
	message string
}

type schedulingCapacityState = modelGroupConditionState

type modelGroupStatusState struct {
	phase        inferencev1alpha1.ModelGroupPhase
	materialized modelGroupConditionState
	available    bool
	scheduling   schedulingCapacityState
}

func schedulingNotEvaluated() schedulingCapacityState {
	return schedulingCapacityState{status: metav1.ConditionUnknown, reason: "NotEvaluated", message: "Scheduling capacity was not evaluated"}
}

func modelGroupFailureState(err error) modelGroupStatusState {
	return modelGroupStatusState{
		phase:        inferencev1alpha1.ModelGroupPhaseFailed,
		materialized: modelGroupConditionState{status: metav1.ConditionFalse, reason: "UnsupportedProfile", message: err.Error()},
		scheduling:   schedulingNotEvaluated(),
	}
}

func modelGroupMaterializedState(available bool, scheduling schedulingCapacityState) modelGroupStatusState {
	phase := inferencev1alpha1.ModelGroupPhaseProvisioning
	if available {
		phase = inferencev1alpha1.ModelGroupPhaseReady
	}
	return modelGroupStatusState{
		phase:        phase,
		materialized: modelGroupConditionState{status: metav1.ConditionTrue, reason: "Applied", message: "Group workload was materialized"},
		available:    available,
		scheduling:   scheduling,
	}
}

// schedulingCapacity reports insufficient capacity only from the scheduler's
// explicit Unschedulable signal; image pulls and slow startup remain unknown.
func (reconciler *ModelGroupReconciler) schedulingCapacity(ctx context.Context, group *inferencev1alpha1.ModelGroup) (schedulingCapacityState, error) {
	var pods corev1.PodList
	if err := reconciler.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels(modelGroupLabels(group))); err != nil {
		return schedulingCapacityState{}, fmt.Errorf("list Group Pods: %w", err)
	}
	scheduled := false
	for index := range pods.Items {
		for _, condition := range pods.Items[index].Status.Conditions {
			if condition.Type != corev1.PodScheduled {
				continue
			}
			if condition.Status == corev1.ConditionFalse && condition.Reason == corev1.PodReasonUnschedulable {
				return schedulingCapacityState{status: metav1.ConditionFalse, reason: "InsufficientCapacity", message: "The Kubernetes scheduler reported the Group Pod as Unschedulable"}, nil
			}
			if condition.Status == corev1.ConditionTrue {
				scheduled = true
			}
		}
	}
	if scheduled {
		return schedulingCapacityState{status: metav1.ConditionTrue, reason: "Scheduled", message: "The Group Pod was scheduled"}, nil
	}
	return schedulingCapacityState{status: metav1.ConditionUnknown, reason: "WaitingForScheduling", message: "The Group Pod has not reported a scheduling result"}, nil
}

func modelGroupDeploymentAvailable(deployment *appsv1.Deployment) bool {
	if deployment.Status.ObservedGeneration < deployment.Generation || deployment.Status.AvailableReplicas != 1 {
		return false
	}
	for _, condition := range deployment.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

func (reconciler *ModelGroupReconciler) updateStatus(ctx context.Context, group *inferencev1alpha1.ModelGroup, state modelGroupStatusState) error {
	base := group.DeepCopy()
	group.Status.Phase = state.phase
	group.Status.TotalMembers = group.Spec.MemberCount
	if state.available {
		group.Status.ReadyMembers = group.Spec.MemberCount
	} else {
		group.Status.ReadyMembers = 0
	}
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionWorkloadMaterialized, Status: state.materialized.status, Reason: state.materialized.reason, Message: state.materialized.message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionSchedulingCapacity, Status: state.scheduling.status, Reason: state.scheduling.reason, Message: state.scheduling.message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionWorkloadAvailable, Status: conditionStatus(state.available), Reason: availabilityReason(state.available), Message: availabilityMessage(state.available), ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: conditionStatus(state.available), Reason: availabilityReason(state.available), Message: readinessMessage(state.available), ObservedGeneration: group.Generation})
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelGroup status: %w", err)
	}
	return nil
}

func availabilityReason(available bool) string {
	if available {
		return "Available"
	}
	return "Unavailable"
}

func availabilityMessage(available bool) string {
	if available {
		return "The Group Deployment is available"
	}
	return "The Group Deployment is not yet available"
}

func readinessMessage(ready bool) string {
	if ready {
		return "The group-local model server and vLLM EngineCore are ready"
	}
	return "The group-local model server or vLLM EngineCore is not yet ready"
}
