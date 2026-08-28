// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles one KVGroup into a single Mooncake client workload.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"math"
	"reflect"
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
)

type KVGroupReconciler struct{ client.Client }

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
	deployment, service, pvc, networkPolicy, err := desiredKVGroupResources(group)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDegraded, false, "InvalidIntent", err.Error())
	}
	for _, object := range []client.Object{pvc, deployment, service, networkPolicy} {
		if err := reconciler.applyOwned(ctx, group, object); err != nil {
			statusErr := reconciler.updateStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDegraded, false, "ApplyFailed", err.Error())
			return ctrl.Result{}, errors.Join(err, statusErr)
		}
	}
	current := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(deployment), current); err != nil {
		return ctrl.Result{}, err
	}
	ready := frontendDeploymentAvailable(current)
	phase := inferencev1alpha1.KVGroupPhaseProvisioning
	if ready {
		phase = inferencev1alpha1.KVGroupPhaseReady
	}
	return ctrl.Result{}, reconciler.updateStatus(ctx, group, phase, ready, clientPodReason(ready), clientPodMessage(ready))
}

func kvGroupWorkloadName(group *inferencev1alpha1.KVGroup) string {
	return kvChildName(group.Name, string(group.UID))
}

// One KVGroup materializes a single Mooncake client together with its offload disk,
// RPC Service, and namespace-scoped network boundary as one owned resource set.
func desiredKVGroupResources(group *inferencev1alpha1.KVGroup) (*appsv1.Deployment, *corev1.Service, *corev1.PersistentVolumeClaim, *networkingv1.NetworkPolicy, error) {
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
	if group.Spec.Client.Disk.Size == "" || group.Spec.Client.RDMAResourceName == "" {
		return nil, nil, nil, nil, fmt.Errorf("KVGroup disk and RDMA resource are required for standalone Store offload")
	}
	rdmaCount := *resource.NewQuantity(int64(group.Spec.Client.RDMAResourceCount), resource.DecimalSI)
	rdmaName := corev1.ResourceName(group.Spec.Client.RDMAResourceName)
	requests[rdmaName], limits[rdmaName] = rdmaCount, rdmaCount
	labels := map[string]string{kvGroupLabel: kvLabelValue(group.Name), kvServiceLabel: kvLabelValue(group.Spec.KVPoolRef.Name)}
	workloadName := kvGroupWorkloadName(group)
	pvcName := kvChildName(group.Name+"-offload", string(group.UID))
	port, replicas := group.Spec.Client.Port, int32(1)
	automountToken, allowPrivilegeEscalation, readOnlyRootFilesystem := false, false, true
	args := []string{
		fmt.Sprintf("--master_server_address=%s:%d", group.Spec.MasterServiceDNS, group.Spec.MasterRPCPort),
		"--host=$(POD_IP)", fmt.Sprintf("--port=%d", port), "--protocol=rdma",
		fmt.Sprintf("--global_segment_size=%s", group.Spec.Client.MemoryCapacityBytes), "--enable_offload=true", "--metadata_server=P2PHANDSHAKE",
	}
	pvc := &corev1.PersistentVolumeClaim{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "PersistentVolumeClaim"},
		ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: group.Namespace, Labels: labels, Annotations: map[string]string{kvGroupDiskRetentionAnnotation: string(group.Spec.Client.Disk.RetentionPolicy)}},
		Spec:       corev1.PersistentVolumeClaimSpec{AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}, Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(string(group.Spec.Client.Disk.Size))}}},
	}
	if group.Spec.Client.Disk.StorageClassName != "" {
		pvc.Spec.StorageClassName = &group.Spec.Client.Disk.StorageClassName
	}
	container := corev1.Container{
		Name: "client", Image: group.Spec.Client.Image, Command: []string{"mooncake_client"}, Args: args,
		Ports: []corev1.ContainerPort{{Name: "rpc", ContainerPort: port, Protocol: corev1.ProtocolTCP}},
		Env: []corev1.EnvVar{
			{Name: "POD_IP", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
			{Name: "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH", Value: "/data/mooncake-offload"},
			{Name: "MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR", Value: "bucket_storage_backend"},
			{Name: "MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES", Value: string(group.Spec.Client.Disk.Size)},
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
			}, SecurityContext: &corev1.PodSecurityContext{SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}}, Containers: []corev1.Container{container}},
		}},
	}
	service := &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"}, ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: labels}, Spec: corev1.ServiceSpec{Type: corev1.ServiceTypeClusterIP, Selector: labels, Ports: []corev1.ServicePort{{Name: "rpc", Port: port, TargetPort: intstr.FromString("rpc")}}}}
	// Mooncake RDMA can use provider-specific dynamic paths. Until a fixed port
	// configuration is verified, namespace is the trust boundary; all namespace Pods
	// may reach client Pods rather than accidentally blocking RDMA data traffic.
	networkPolicy := &networkingv1.NetworkPolicy{TypeMeta: metav1.TypeMeta{APIVersion: networkingv1.SchemeGroupVersion.String(), Kind: "NetworkPolicy"}, ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: labels}, Spec: networkingv1.NetworkPolicySpec{PodSelector: metav1.LabelSelector{MatchLabels: labels}, PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}, Ingress: []networkingv1.NetworkPolicyIngressRule{{From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{}}}}}}}
	return deployment, service, pvc, networkPolicy, nil
}

// applyOwned creates or updates one KVGroup-owned resource while preserving provider-assigned fields.
func (reconciler *KVGroupReconciler) applyOwned(ctx context.Context, group *inferencev1alpha1.KVGroup, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
			return err
		}
		return reconciler.Create(ctx, desired)
	}
	if err != nil {
		return err
	}
	if !metav1.IsControlledBy(current, group) {
		return fmt.Errorf("%T %q is not controlled by KVGroup", current, current.GetName())
	}
	if desiredService, ok := desired.(*corev1.Service); ok {
		existing := current.(*corev1.Service)
		desiredService.Spec.ClusterIP, desiredService.Spec.ClusterIPs, desiredService.Spec.IPFamilies, desiredService.Spec.IPFamilyPolicy = existing.Spec.ClusterIP, existing.Spec.ClusterIPs, existing.Spec.IPFamilies, existing.Spec.IPFamilyPolicy
	}
	if desiredPVC, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		existing := current.(*corev1.PersistentVolumeClaim)
		if retention := existing.Annotations[kvGroupDiskRetentionAnnotation]; retention != "" {
			desiredPVC.Annotations[kvGroupDiskRetentionAnnotation] = retention
		}
	}
	desired.SetResourceVersion(current.GetResourceVersion())
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return err
	}
	return reconciler.Update(ctx, desired)
}

// Delete client networking and workload resources before releasing the finalizer. The disk
// PVC follows its retention policy; this lifecycle does not claim block migration or GC.
func (reconciler *KVGroupReconciler) reconcileDelete(ctx context.Context, group *inferencev1alpha1.KVGroup) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(group, kvGroupFinalizer) {
		return ctrl.Result{}, nil
	}
	_ = reconciler.updateStatus(ctx, group, inferencev1alpha1.KVGroupPhaseDraining, false, "Draining", "Deleting client infrastructure with bounded process shutdown; Mooncake block migration and GC are not performed")
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

// updateStatus publishes the observed client workload phase and readiness conditions.
func (reconciler *KVGroupReconciler) updateStatus(ctx context.Context, group *inferencev1alpha1.KVGroup, phase inferencev1alpha1.KVGroupPhase, ready bool, reason, message string) error {
	base := group.DeepCopy()
	group.Status.ObservedGeneration = group.Generation
	group.Status.Phase = phase
	group.Status.RequestedMemoryCapacityBytes = group.Spec.Client.MemoryCapacityBytes
	group.Status.RequestedDiskCapacityBytes = group.Spec.Client.Disk.Size
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionClientPodReady, Status: conditionStatus(ready), Reason: reason, Message: message, ObservedGeneration: group.Generation})
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
