// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles one KVGroup into a single Mooncake client workload.

package controllers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
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
)

const (
	kvGroupFinalizer               = "inference.foretoken.io/kvgroup-protection"
	kvGroupLabel                   = "inference.foretoken.io/kv-group"
	kvGroupDiskRetentionAnnotation = "inference.foretoken.io/disk-retention"
	conditionClientPodReady        = "ClientPodReady"
	conditionStorageRegistered     = "StorageRegistered"
	conditionStorageDrained        = "StorageDrained"
	storageRegistrationPath        = "/registration"
	storageDrainQuiescePath        = "/cache-loss/quiesce"
	storageDrainStatusPath         = "/cache-loss/status"
	masterRegistrationPath         = "/api/v1/clients/registration"
	masterCacheLossPath            = "/api/v1/clients/cache_loss"
	storageRegistrationRequeue     = 10 * time.Second
	storageDrainPoll               = 5 * time.Second
)

type kvGroupCondition struct {
	ready   bool
	reason  string
	message string
}

type kvClientRegistrationResponse struct {
	ClientID           string   `json:"client_id"`
	MemorySegmentIDs   []string `json:"memory_segment_ids"`
	SSDEnabled         bool     `json:"ssd_enabled"`
	CacheLossSupported bool     `json:"cache_loss_supported"`
}

// kvClientCacheLossStatus is the client's view of its exit: after quiesce it admits no new
// SSD reads and its memory segments are leaving through Master's graceful unmount, while
// the counters show work that readers or background tasks still hold.
type kvClientCacheLossStatus struct {
	ClientID                 string `json:"client_id"`
	Quiesced                 bool   `json:"quiesced"`
	HeartbeatInFlight        bool   `json:"heartbeat_in_flight"`
	MetadataRescanInFlight   bool   `json:"metadata_rescan_in_flight"`
	ActiveBatchGets          uint64 `json:"active_batch_gets"`
	AllocatedBatches         uint64 `json:"allocated_batches"`
	MemorySegmentsMounted    uint64 `json:"memory_segments_mounted"`
	MemorySegmentsUnmounting uint64 `json:"memory_segments_unmounting"`
	FenceToken               string `json:"fence_token"`
}

type kvMasterCacheLossRequest struct {
	ClientID   string `json:"client_id"`
	FenceToken string `json:"fence_token"`
}

type kvMasterRegistrationResponse struct {
	ClientID                 string `json:"client_id"`
	SSDRegistered            bool   `json:"ssd_registered"`
	SSDReportedCapacityBytes int64  `json:"ssd_reported_capacity_bytes"`
}

type kvSegmentDetail struct {
	SegmentID              string `json:"segment_id"`
	ClientID               string `json:"client_id"`
	Protocol               string `json:"protocol"`
	Status                 string `json:"status"`
	AllocatorCapacityBytes uint64 `json:"allocator_capacity_bytes"`
}

type kvSegmentsDetailResponse struct {
	Segments []kvSegmentDetail `json:"segments"`
}

type managementHTTPError struct {
	status int
}

func (err *managementHTTPError) Error() string {
	return fmt.Sprintf("endpoint returned HTTP %d", err.status)
}

type KVGroupReconciler struct {
	client.Client
	HTTPClient            *http.Client
	ControlPlaneNamespace string
}

// SetupWithManager registers KVGroup reconciliation for its owned client infrastructure.
func (reconciler *KVGroupReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.KVGroup{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Complete(reconciler)
}

// Reconcile materializes one KVGroup client workload and publishes its readiness.
func (reconciler *KVGroupReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	group := new(inferencev1alpha1.KVGroup)
	if err := reconciler.Get(ctx, request.NamespacedName, group); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !group.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, group)
	}
	pool := new(inferencev1alpha1.KVPool)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.KVPoolRef.Name}, pool); err != nil {
		return ctrl.Result{}, fmt.Errorf("get owning KVPool: %w", err)
	}
	if group.Spec.KVPoolRef.UID != string(pool.UID) || !metav1.IsControlledBy(group, pool) {
		return ctrl.Result{}, fmt.Errorf("KVGroup %q is not owned by its referenced KVPool", group.Name)
	}
	if !controllerutil.ContainsFinalizer(group, kvGroupFinalizer) {
		base := group.DeepCopy()
		controllerutil.AddFinalizer(group, kvGroupFinalizer)
		if err := reconciler.Patch(ctx, group, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}
	deployment, service, pvc, networkPolicy, err := desiredKVGroupResources(group, reconciler.ControlPlaneNamespace)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDegraded, false, false, "InvalidIntent", err.Error(), kvGroupCondition{reason: "InvalidIntent", message: err.Error()})
	}
	for _, object := range []client.Object{pvc, deployment, service, networkPolicy} {
		if err := reconciler.applyOwned(ctx, group, object); err != nil {
			statusErr := reconciler.updateStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDegraded, false, false, "ApplyFailed", err.Error(), kvGroupCondition{reason: "ApplyFailed", message: err.Error()})
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
	}
	current := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(deployment), current); err != nil {
		return ctrl.Result{}, err
	}
	podReady := frontendDeploymentAvailable(current)
	storageEnabled := storageRegistrationEnabled(group)
	storageReady := true
	storageCondition := kvGroupCondition{}
	if storageEnabled {
		storageReady = false
		storageCondition = kvGroupCondition{reason: "ClientPodNotReady", message: "Mooncake client Pod is not Kubernetes-ready"}
		if podReady {
			storageReady, storageCondition = reconciler.checkStorageRegistration(ctx, group)
		}
	}
	ready := podReady && storageReady
	phase := inferencev1alpha1.KVGroupPhaseProvisioning
	if ready {
		phase = inferencev1alpha1.KVGroupPhaseReady
	}
	result := ctrl.Result{}
	if storageEnabled {
		result.RequeueAfter = storageRegistrationRequeue
	}
	readyReason, readyMessage := clientPodReason(podReady), clientPodMessage(podReady)
	if storageEnabled && podReady && !storageReady {
		readyReason, readyMessage = storageCondition.reason, storageCondition.message
	}
	return result, reconciler.updateStatus(ctx, group, phase, podReady, ready, readyReason, readyMessage, storageCondition)
}

func kvGroupWorkloadName(group *inferencev1alpha1.KVGroup) string {
	return kvChildName(group.Name, string(group.UID))
}

// One KVGroup materializes a single Mooncake client together with its offload disk,
// RPC Service, and namespace-scoped network boundary as one owned resource set.
func desiredKVGroupResources(group *inferencev1alpha1.KVGroup, controlPlaneNamespace string) (*appsv1.Deployment, *corev1.Service, *corev1.PersistentVolumeClaim, *networkingv1.NetworkPolicy, error) {
	requests, limits, err := kvResources(group.Spec.Client.Resources)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	drain, err := time.ParseDuration(string(group.Spec.Timeouts.Drain))
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("parse KVGroup drain timeout: %w", err)
	}
	if drain <= 0 {
		return nil, nil, nil, nil, fmt.Errorf("KVGroup drain timeout must be positive")
	}
	terminationGracePeriodSeconds := int64(math.Ceil(drain.Seconds()))
	if group.Spec.Client.Disk.Size == "" {
		return nil, nil, nil, nil, fmt.Errorf("KVGroup disk is required for standalone Store offload")
	}
	if group.Spec.Client.Protocol == "rdma" {
		if group.Spec.Client.RDMAResourceName == "" {
			return nil, nil, nil, nil, fmt.Errorf("KVGroup RDMA resource is required for RDMA transport")
		}
		rdmaCount := *resource.NewQuantity(int64(group.Spec.Client.RDMAResourceCount), resource.DecimalSI)
		rdmaName := corev1.ResourceName(group.Spec.Client.RDMAResourceName)
		requests[rdmaName], limits[rdmaName] = rdmaCount, rdmaCount
	}
	labels := map[string]string{kvGroupLabel: kvLabelValue(group.Name), kvServiceLabel: kvLabelValue(group.Spec.KVPoolRef.Name)}
	workloadName := kvGroupWorkloadName(group)
	pvcName := kvChildName(group.Name+"-offload", string(group.UID))
	port, replicas := group.Spec.Client.Port, int32(1)
	registrationEnabled := storageRegistrationEnabled(group)
	registrationPort := int32(0)
	if registrationEnabled {
		registrationPort = storageManagementPort(group)
	}
	automountToken, allowPrivilegeEscalation, readOnlyRootFilesystem := false, false, true
	args := []string{
		fmt.Sprintf("--master_server_address=%s:%d", group.Spec.MasterServiceDNS, group.Spec.MasterRPCPort),
		"--host=$(POD_IP)", fmt.Sprintf("--port=%d", port), "--protocol=" + group.Spec.Client.Protocol,
		fmt.Sprintf("--global_segment_size=%s", group.Spec.Client.MemoryCapacityBytes), "--enable_offload=true", "--metadata_server=P2PHANDSHAKE",
	}
	if registrationEnabled {
		args = append(args, "--enable_http_server=true", fmt.Sprintf("--http_port=%d", registrationPort))
	}
	pvc := &corev1.PersistentVolumeClaim{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "PersistentVolumeClaim"},
		ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: group.Namespace, Labels: labels, Annotations: map[string]string{kvGroupDiskRetentionAnnotation: string(group.Spec.Client.Disk.RetentionPolicy)}},
		Spec:       corev1.PersistentVolumeClaimSpec{AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}, Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(string(group.Spec.Client.Disk.Size))}}},
	}
	if group.Spec.Client.Disk.StorageClassName != "" {
		pvc.Spec.StorageClassName = &group.Spec.Client.Disk.StorageClassName
	}
	containerPorts := []corev1.ContainerPort{{Name: "rpc", ContainerPort: port, Protocol: corev1.ProtocolTCP}}
	servicePorts := []corev1.ServicePort{{Name: "rpc", Port: port, TargetPort: intstr.FromString("rpc")}}
	if registrationEnabled {
		containerPorts = append(containerPorts, corev1.ContainerPort{Name: "management", ContainerPort: registrationPort, Protocol: corev1.ProtocolTCP})
		servicePorts = append(servicePorts, corev1.ServicePort{Name: "management", Port: registrationPort, TargetPort: intstr.FromString("management")})
	}
	container := corev1.Container{
		Name: "client", Image: group.Spec.Client.Image, Command: []string{"mooncake_client"}, Args: args,
		Ports: containerPorts,
		Env: []corev1.EnvVar{
			{Name: "POD_IP", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
			{Name: "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH", Value: "/data/mooncake-offload"},
			{Name: "MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR", Value: "bucket_storage_backend"},
			// Capacity reporting and bucket storage use the same disk budget.
			{Name: "MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", Value: string(group.Spec.Client.Disk.Size)},
			{Name: "MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE", Value: string(group.Spec.Client.Disk.Size)},
		},
		Resources:       corev1.ResourceRequirements{Requests: requests, Limits: limits},
		VolumeMounts:    []corev1.VolumeMount{{Name: "offload-storage", MountPath: "/data/mooncake-offload"}, {Name: "shm", MountPath: "/dev/shm"}},
		SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: &allowPrivilegeEscalation, ReadOnlyRootFilesystem: &readOnlyRootFilesystem, Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
		ReadinessProbe:  tcpProbe("rpc", 10), LivenessProbe: tcpProbe("rpc", 15),
	}
	deployment := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{Replicas: &replicas, Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType}, Selector: &metav1.LabelSelector{MatchLabels: labels}, Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: labels},
			Spec: corev1.PodSpec{AutomountServiceAccountToken: &automountToken, TerminationGracePeriodSeconds: &terminationGracePeriodSeconds, NodeSelector: group.Spec.Client.NodeSelector, Volumes: []corev1.Volume{
				{Name: "offload-storage", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: pvcName}}},
				{Name: "shm", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory}}},
			}, SecurityContext: &corev1.PodSecurityContext{FSGroup: group.Spec.Client.FSGroup, SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}}, Containers: []corev1.Container{container}},
		}},
	}
	service := &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"}, ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: labels}, Spec: corev1.ServiceSpec{Type: corev1.ServiceTypeClusterIP, Selector: labels, Ports: servicePorts}}
	// Keep namespace ingress open because the provider may use dynamic data paths.
	ingress := []networkingv1.NetworkPolicyIngressRule{{From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{}}}}}
	if registrationEnabled {
		if controlPlaneNamespace == "" {
			return nil, nil, nil, nil, fmt.Errorf("control-plane namespace is required for storage registration")
		}
		protocol := corev1.ProtocolTCP
		managementNetworkPort := intstr.FromString("management")
		ingress = append(ingress, networkingv1.NetworkPolicyIngressRule{
			From: []networkingv1.NetworkPolicyPeer{{
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": controlPlaneNamespace}},
				PodSelector:       &metav1.LabelSelector{MatchLabels: map[string]string{controlPlanePodLabel: controlPlanePodLabelValue}},
			}},
			Ports: []networkingv1.NetworkPolicyPort{{Port: &managementNetworkPort, Protocol: &protocol}},
		})
	}
	networkPolicy := &networkingv1.NetworkPolicy{TypeMeta: metav1.TypeMeta{APIVersion: networkingv1.SchemeGroupVersion.String(), Kind: "NetworkPolicy"}, ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: labels}, Spec: networkingv1.NetworkPolicySpec{PodSelector: metav1.LabelSelector{MatchLabels: labels}, PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}, Ingress: ingress}}
	return deployment, service, pvc, networkPolicy, nil
}

// applyOwned creates or updates one KVGroup-owned resource while preserving provider-assigned fields.
func (reconciler *KVGroupReconciler) applyOwned(ctx context.Context, group *inferencev1alpha1.KVGroup, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current)
	missing := apierrors.IsNotFound(err)
	if err != nil && !missing {
		return err
	}
	if !missing && !metav1.IsControlledBy(current, group) {
		return fmt.Errorf("%T %q is not controlled by KVGroup", current, current.GetName())
	}
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return err
	}
	if _, ok := desired.(*appsv1.Deployment); ok {
		// The stable field owner preserves the Deployment controller's revision annotation.
		return reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner("foretoken-kvgroup"), client.ForceOwnership)
	}
	if missing {
		return reconciler.Create(ctx, desired)
	}
	if desiredService, ok := desired.(*corev1.Service); ok {
		existing := current.(*corev1.Service)
		desiredService.Spec.ClusterIP, desiredService.Spec.ClusterIPs, desiredService.Spec.IPFamilies, desiredService.Spec.IPFamilyPolicy = existing.Spec.ClusterIP, existing.Spec.ClusterIPs, existing.Spec.IPFamilies, existing.Spec.IPFamilyPolicy
	}
	if desiredPVC, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		existing := current.(*corev1.PersistentVolumeClaim)
		preservePVCBindingAndMetadata(desiredPVC, existing)
		if retention := existing.Annotations[kvGroupDiskRetentionAnnotation]; retention != "" {
			desiredPVC.Annotations[kvGroupDiskRetentionAnnotation] = retention
		}
	}
	desired.SetResourceVersion(current.GetResourceVersion())
	return reconciler.Update(ctx, desired)
}

// reconcileDelete attempts provider cleanup before removing client infrastructure.
// Phase Terminating persists the outcome so cleanup can continue after management
// endpoints disappear. Unsupported providers and expired drain budgets use bounded
// process termination; the outcome does not guarantee completion of direct-memory reads.
func (reconciler *KVGroupReconciler) reconcileDelete(ctx context.Context, group *inferencev1alpha1.KVGroup) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(group, kvGroupFinalizer) {
		return ctrl.Result{}, nil
	}
	if group.Status.Phase != inferencev1alpha1.KVGroupPhaseTerminating {
		var drained *kvGroupCondition
		if storageRegistrationEnabled(group) {
			condition, done := reconciler.drainStorage(ctx, group)
			drained = &condition
			if !done {
				return ctrl.Result{RequeueAfter: storageDrainPoll}, reconciler.updateDeletionStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDraining, drained)
			}
		}
		if err := reconciler.updateDeletionStatus(ctx, group, inferencev1alpha1.KVGroupPhaseTerminating, drained); err != nil {
			return ctrl.Result{}, err
		}
	}
	pending := false
	for _, object := range []client.Object{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: kvGroupWorkloadName(group), Namespace: group.Namespace}}, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: kvGroupWorkloadName(group), Namespace: group.Namespace}}, &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: kvGroupWorkloadName(group), Namespace: group.Namespace}}} {
		present, err := reconciler.deleteIfPresent(ctx, object)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || present
	}
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: kvChildName(group.Name+"-offload", string(group.UID)), Namespace: group.Namespace}}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(pvc), pvc); err == nil {
		if pvc.Annotations[kvGroupDiskRetentionAnnotation] == string(inferencev1alpha1.RetentionPolicyRetain) {
			if err := reconciler.releaseDiskPVC(ctx, group, pvc); err != nil {
				return ctrl.Result{}, err
			}
		} else {
			present, err := reconciler.deleteIfPresent(ctx, pvc)
			if err != nil {
				return ctrl.Result{}, err
			}
			pending = pending || present
		}
	} else if !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	if pending {
		return ctrl.Result{Requeue: true}, nil
	}
	base := group.DeepCopy()
	controllerutil.RemoveFinalizer(group, kvGroupFinalizer)
	return ctrl.Result{}, reconciler.Patch(ctx, group, client.MergeFrom(base))
}

func (reconciler *KVGroupReconciler) deleteIfPresent(ctx context.Context, object client.Object) (bool, error) {
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	if err := reconciler.Delete(ctx, object); err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return true, nil
}

// releaseDiskPVC removes KVGroup ownership from a retained offload PVC.
func (reconciler *KVGroupReconciler) releaseDiskPVC(ctx context.Context, group *inferencev1alpha1.KVGroup, pvc *corev1.PersistentVolumeClaim) error {
	base := pvc.DeepCopy()
	owners := pvc.OwnerReferences[:0]
	for _, owner := range pvc.OwnerReferences {
		if owner.UID != group.UID {
			owners = append(owners, owner)
		}
	}
	pvc.OwnerReferences = owners
	if reflect.DeepEqual(base.OwnerReferences, pvc.OwnerReferences) {
		return nil
	}
	return reconciler.Patch(ctx, pvc, client.MergeFrom(base))
}

// drainStorage runs one idempotent pass of the client's cache-loss exit and reports whether
// it is settled. Every pass repeats each call, so nothing is persisted before the outcome is
// final. The drain budget starts at the deletion timestamp; once it elapses, deletion
// proceeds and the condition states what was still pending.
func (reconciler *KVGroupReconciler) drainStorage(ctx context.Context, group *inferencev1alpha1.KVGroup) (kvGroupCondition, bool) {
	// The timeout was validated when the workload was materialized.
	drain, _ := time.ParseDuration(string(group.Spec.Timeouts.Drain))
	expired := time.Now().After(group.DeletionTimestamp.Add(drain))
	pending := func(condition kvGroupCondition) (kvGroupCondition, bool) {
		if expired {
			return kvGroupCondition{reason: "DrainTimedOut", message: "Drain timeout elapsed; deleting with cache loss: " + condition.message}, true
		}
		return condition, false
	}
	if group.Spec.MasterAdminPort == 0 {
		return kvGroupCondition{reason: "Unsupported", message: "Master admin port is not resolved for storage drain"}, true
	}
	clientBase := fmt.Sprintf("http://%s.%s.svc:%d", kvGroupWorkloadName(group), group.Namespace, storageManagementPort(group))
	var registration kvClientRegistrationResponse
	if err := reconciler.providerJSON(ctx, http.MethodGet, clientBase+storageRegistrationPath, nil, &registration); err != nil {
		condition := storageRegistrationCondition(err)
		if condition.reason == "Unsupported" {
			return condition, true
		}
		return pending(condition)
	}
	if !registration.CacheLossSupported {
		return kvGroupCondition{reason: "Unsupported", message: "Store client image has no cache-loss drain protocol; Master keeps its metadata until the client expires"}, true
	}
	registeredClientID, valid := mooncakeID(registration.ClientID)
	if !valid {
		return pending(kvGroupCondition{reason: "InvalidResponse", message: "Store registration returned an invalid client identity"})
	}
	var quiesce kvClientCacheLossStatus
	if err := reconciler.providerJSON(ctx, http.MethodPost, clientBase+storageDrainQuiescePath, nil, &quiesce); err != nil {
		return pending(storageRegistrationCondition(err))
	}
	clientID, valid := mooncakeID(quiesce.ClientID)
	if !valid || quiesce.FenceToken == "" {
		return pending(kvGroupCondition{reason: "InvalidResponse", message: "Cache-loss quiesce returned an invalid client identity or fence"})
	}
	if clientID != registeredClientID {
		return pending(kvGroupCondition{reason: "ClientRestarted", message: "Store client changed during drain; retrying with its current identity"})
	}
	masterURL := fmt.Sprintf("http://%s:%d%s", group.Spec.MasterServiceDNS, group.Spec.MasterAdminPort, masterCacheLossPath)
	if err := reconciler.providerJSON(ctx, http.MethodPost, masterURL, kvMasterCacheLossRequest{ClientID: clientID, FenceToken: quiesce.FenceToken}, nil); err != nil {
		// Master answers 404 both for a missing SSD membership and for an image without the
		// endpoint; the next pass re-fences through the client, and the budget bounds the rest.
		return pending(kvGroupCondition{reason: "Unavailable", message: "Master did not drop the client's SSD metadata: " + err.Error()})
	}
	var status kvClientCacheLossStatus
	if err := reconciler.providerJSON(ctx, http.MethodGet, clientBase+storageDrainStatusPath, nil, &status); err != nil {
		return pending(storageRegistrationCondition(err))
	}
	statusClientID, valid := mooncakeID(status.ClientID)
	if !valid || statusClientID != clientID || !status.Quiesced || status.FenceToken != quiesce.FenceToken {
		return pending(kvGroupCondition{reason: "Draining", message: "Store client identity or fence changed during drain; quiesce is repeated"})
	}
	if status.ActiveBatchGets > 0 || status.AllocatedBatches > 0 || status.HeartbeatInFlight || status.MetadataRescanInFlight || status.MemorySegmentsMounted > 0 || status.MemorySegmentsUnmounting > 0 {
		return pending(kvGroupCondition{reason: "Draining", message: fmt.Sprintf("SSD metadata dropped; waiting for %d SSD reads, %d retained SSD buffers, %d memory segments, heartbeat %t, metadata rescan %t", status.ActiveBatchGets, status.AllocatedBatches, status.MemorySegmentsMounted+status.MemorySegmentsUnmounting, status.HeartbeatInFlight, status.MetadataRescanInFlight)})
	}
	return kvGroupCondition{ready: true, reason: "Drained", message: "SSD metadata removed, tracked SSD readers released, and memory unmount bookkeeping completed"}, true
}

// updateDeletionStatus publishes drain progress and outcome while the KVGroup is deleting.
func (reconciler *KVGroupReconciler) updateDeletionStatus(ctx context.Context, group *inferencev1alpha1.KVGroup, phase inferencev1alpha1.KVGroupPhase, drained *kvGroupCondition) error {
	base := group.DeepCopy()
	group.Status.Phase = phase
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionFalse, Reason: "Deleting", Message: "KVGroup is being deleted", ObservedGeneration: group.Generation})
	if drained != nil {
		meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionStorageDrained, Status: conditionStatus(drained.ready), Reason: drained.reason, Message: drained.message, ObservedGeneration: group.Generation})
	}
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	return reconciler.Status().Patch(ctx, group, client.MergeFrom(base))
}

func storageRegistrationEnabled(group *inferencev1alpha1.KVGroup) bool {
	return group.Spec.Client.StorageRegistration != nil && group.Spec.Client.StorageRegistration.Enabled
}

// storageManagementPort is the client HTTP port for registration and drain calls.
func storageManagementPort(group *inferencev1alpha1.KVGroup) int32 {
	if port := group.Spec.Client.StorageRegistration.Port; port != 0 {
		return port
	}
	return inferencev1alpha1.DefaultStorageRegistrationPort
}

// mooncakeID validates and canonicalizes Mooncake's two-uint64 decimal identifier.
func mooncakeID(value string) (string, bool) {
	if value == "" || strings.TrimSpace(value) != value {
		return "", false
	}
	parts := strings.Split(value, "-")
	if len(parts) != 2 {
		return "", false
	}
	values := [2]uint64{}
	for index, part := range parts {
		if part == "" {
			return "", false
		}
		for _, character := range part {
			if character < '0' || character > '9' {
				return "", false
			}
		}
		parsed, err := strconv.ParseUint(part, 10, 64)
		if err != nil {
			return "", false
		}
		values[index] = parsed
	}
	return fmt.Sprintf("%d-%d", values[0], values[1]), true
}

// checkStorageRegistration verifies the client identity and SSD capacity against Master.
func (reconciler *KVGroupReconciler) checkStorageRegistration(ctx context.Context, group *inferencev1alpha1.KVGroup) (bool, kvGroupCondition) {
	if !storageRegistrationEnabled(group) {
		return true, kvGroupCondition{}
	}
	clientURL := fmt.Sprintf("http://%s.%s.svc:%d%s", kvGroupWorkloadName(group), group.Namespace, storageManagementPort(group), storageRegistrationPath)
	var clientResponse kvClientRegistrationResponse
	if err := reconciler.providerJSON(ctx, http.MethodGet, clientURL, nil, &clientResponse); err != nil {
		return false, storageRegistrationCondition(err)
	}
	clientID, valid := mooncakeID(clientResponse.ClientID)
	if !valid {
		return false, kvGroupCondition{reason: "Unsupported", message: "Storage registration returned an invalid client ID"}
	}
	if len(clientResponse.MemorySegmentIDs) == 0 {
		return false, kvGroupCondition{reason: "Unsupported", message: "Storage registration returned no memory segments"}
	}
	seenSegments := make(map[string]struct{}, len(clientResponse.MemorySegmentIDs))
	for _, segmentID := range clientResponse.MemorySegmentIDs {
		segment, valid := mooncakeID(segmentID)
		if !valid {
			return false, kvGroupCondition{reason: "Unsupported", message: "Storage registration returned an invalid memory segment ID"}
		}
		if _, duplicate := seenSegments[segment]; duplicate {
			return false, kvGroupCondition{reason: "Unsupported", message: "Storage registration returned duplicate memory segments"}
		}
		seenSegments[segment] = struct{}{}
	}
	if !clientResponse.SSDEnabled {
		return false, kvGroupCondition{reason: "Unsupported", message: "Storage registration does not report SSD offload enabled"}
	}
	if group.Spec.MasterAdminPort == 0 {
		return false, kvGroupCondition{reason: "Unsupported", message: "Master admin port is not resolved for storage registration"}
	}
	diskCapacity, err := exactPositiveBytes(group.Spec.Client.Disk.Size)
	if err != nil {
		return false, kvGroupCondition{reason: "Unsupported", message: "KVGroup disk capacity is not a valid byte quantity"}
	}
	masterURL := fmt.Sprintf("http://%s:%d%s?client_id=%s", group.Spec.MasterServiceDNS, group.Spec.MasterAdminPort, masterRegistrationPath, url.QueryEscape(clientID))
	var masterResponse kvMasterRegistrationResponse
	if err := reconciler.providerJSON(ctx, http.MethodGet, masterURL, nil, &masterResponse); err != nil {
		return false, storageRegistrationCondition(err)
	}
	masterID, valid := mooncakeID(masterResponse.ClientID)
	if !valid || masterID != clientID || !masterResponse.SSDRegistered || masterResponse.SSDReportedCapacityBytes != diskCapacity {
		return false, kvGroupCondition{reason: "Unsupported", message: "Master registration does not match client identity or SSD capacity"}
	}
	segmentsURL := fmt.Sprintf("http://%s:%d/get_segments_detail", group.Spec.MasterServiceDNS, group.Spec.MasterAdminPort)
	var segmentsResponse kvSegmentsDetailResponse
	if err := reconciler.providerJSON(ctx, http.MethodGet, segmentsURL, nil, &segmentsResponse); err != nil {
		return false, storageRegistrationCondition(err)
	}
	segmentDetails := make(map[string]kvSegmentDetail, len(segmentsResponse.Segments))
	for _, detail := range segmentsResponse.Segments {
		segmentID, valid := mooncakeID(detail.SegmentID)
		if valid {
			segmentDetails[segmentID] = detail
		}
	}
	for segmentID := range seenSegments {
		detail, present := segmentDetails[segmentID]
		masterSegmentClient, valid := mooncakeID(detail.ClientID)
		if !present || !valid || masterSegmentClient != clientID || detail.Protocol != group.Spec.Client.Protocol || detail.Status != "OK" || detail.AllocatorCapacityBytes == 0 {
			return false, kvGroupCondition{reason: "Unsupported", message: "Master segment details do not match the registered client"}
		}
	}
	return true, kvGroupCondition{ready: true, reason: "Registered", message: "Mooncake client registration and memory segments match Master"}
}

// providerJSON performs one bounded management call against the Store client or Master.
// A nil body sends no payload; a nil target discards the response body.
func (reconciler *KVGroupReconciler) providerJSON(ctx context.Context, method, endpoint string, body, target any) error {
	if reconciler.HTTPClient == nil {
		return errors.New("storage management HTTP client is not configured")
	}
	var payload io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, payload)
	if err != nil {
		return err
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := reconciler.HTTPClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return &managementHTTPError{status: response.StatusCode}
	}
	if target == nil {
		return nil
	}
	if err := json.NewDecoder(response.Body).Decode(target); err != nil {
		return fmt.Errorf("decode management response: %w", err)
	}
	return nil
}

// storageRegistrationCondition turns a provider endpoint failure into a visible status.
func storageRegistrationCondition(err error) kvGroupCondition {
	var httpErr *managementHTTPError
	if errors.As(err, &httpErr) && httpErr.status == http.StatusNotFound {
		return kvGroupCondition{reason: "Unsupported", message: err.Error()}
	}
	return kvGroupCondition{reason: "Unavailable", message: err.Error()}
}

// updateStatus publishes the observed client workload phase and readiness conditions.
func (reconciler *KVGroupReconciler) updateStatus(ctx context.Context, group *inferencev1alpha1.KVGroup, phase inferencev1alpha1.KVGroupPhase, podReady, ready bool, reason, message string, storage kvGroupCondition) error {
	base := group.DeepCopy()
	group.Status.ObservedGeneration = group.Generation
	group.Status.Phase = phase
	group.Status.RequestedMemoryCapacityBytes = group.Spec.Client.MemoryCapacityBytes
	group.Status.RequestedDiskCapacityBytes = group.Spec.Client.Disk.Size
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionClientPodReady, Status: conditionStatus(podReady), Reason: clientPodReason(podReady), Message: clientPodMessage(podReady), ObservedGeneration: group.Generation})
	if storageRegistrationEnabled(group) {
		meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionStorageRegistered, Status: conditionStatus(storage.ready), Reason: storage.reason, Message: storage.message, ObservedGeneration: group.Generation})
	} else {
		meta.RemoveStatusCondition(&group.Status.Conditions, conditionStorageRegistered)
	}
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: conditionStatus(ready), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	return reconciler.Status().Patch(ctx, group, client.MergeFrom(base))
}
func clientPodReason(ready bool) string {
	if ready {
		return "ClientPodReady"
	}
	return "ClientPodNotReady"
}
func clientPodMessage(ready bool) string {
	if ready {
		return "Mooncake client Pod is Kubernetes-ready and reachable through its Service"
	}
	return "Mooncake client Pod is not Kubernetes-ready"
}
