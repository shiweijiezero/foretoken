// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles KVService into its Master infrastructure and controller-owned KVPools.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"reflect"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	kvServiceFinalizer           = "inference.foretoken.io/kvservice-protection"
	conditionInfrastructureReady = "InfrastructureReady"
	conditionKVPoolsMaterialized = "PoolsMaterialized"
	snapshotRetentionAnnotation  = "inference.foretoken.io/snapshot-retention"
)

type KVServiceReconciler struct {
	client.Client
	HTTPClient *http.Client
}

type kvServiceCondition struct {
	ready   bool
	reason  string
	message string
}

type kvServiceStatus struct {
	phase          inferencev1alpha1.KVServicePhase
	infrastructure kvServiceCondition
	pools          kvServiceCondition
	ready          kvServiceCondition
	binding        *inferencev1alpha1.KVServiceBinding
}

// SetupWithManager registers KVService reconciliation for master infrastructure and KVPools.
func (reconciler *KVServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	if reconciler.HTTPClient == nil {
		reconciler.HTTPClient = &http.Client{Timeout: 2 * time.Second}
	}
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.KVService{}).
		Owns(&inferencev1alpha1.KVPool{}).
		Owns(&appsv1.Deployment{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Complete(reconciler)
}

// Reconcile materializes KVService infrastructure and publishes its dependency readiness.
func (reconciler *KVServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	service := new(inferencev1alpha1.KVService)
	if err := reconciler.Get(ctx, request.NamespacedName, service); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !service.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, service)
	}
	if !controllerutil.ContainsFinalizer(service, kvServiceFinalizer) {
		base := service.DeepCopy()
		controllerutil.AddFinalizer(service, kvServiceFinalizer)
		if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add KVService finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}
	desiredPools, err := normalizedKVPoolSpecs(service)
	if err != nil {
		condition := kvServiceCondition{reason: "InvalidConfiguration", message: err.Error()}
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseDegraded,
			infrastructure: condition,
			pools:          condition,
			ready:          condition,
		}))
	}
	binding, err := reconciler.reconcileInfrastructure(ctx, service)
	if err != nil {
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseDegraded,
			infrastructure: kvServiceCondition{reason: "ApplyFailed", message: "Master infrastructure was not fully materialized"},
			pools:          kvServiceCondition{reason: "ApplyFailed", message: "KVPools were not fully materialized"},
			ready:          kvServiceCondition{reason: "InfrastructureNotReady", message: "Master infrastructure is not ready"},
		}))
	}
	infrastructureReady, err := reconciler.infrastructureReady(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	pools, err := reconciler.ownedPools(ctx, service)
	poolsConverged, capacityAvailable := false, false
	if err == nil {
		poolsConverged, capacityAvailable, err = kvPoolState(pools, desiredPools)
	}
	if err != nil {
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseDegraded,
			infrastructure: infrastructureCondition(infrastructureReady),
			pools:          kvServiceCondition{reason: "ObservationFailed", message: "KVPool state could not be observed"},
			ready:          kvServiceCondition{reason: "ObservationFailed", message: "Client availability could not be determined"},
		}))
	}
	// A changed Master entry reaches clients only after its native infrastructure is ready.
	// Existing Pools remain untouched while a new single or HA Master cohort starts.
	var applyErr error
	if infrastructureReady || len(pools) == 0 {
		applyErr = reconciler.reconcilePools(ctx, service, pools, desiredPools)
	}
	ready := infrastructureReady && capacityAvailable
	phase := inferencev1alpha1.KVServicePhaseProgressing
	if ready && poolsConverged {
		phase = inferencev1alpha1.KVServicePhaseReady
	}
	readyCondition := kvServiceCondition{reason: "DependenciesNotReady", message: "Master infrastructure or requested client capacity is not Kubernetes-ready"}
	if ready {
		readyCondition = kvServiceCondition{ready: true, reason: "Available", message: "Master and compatible client infrastructure are Kubernetes-ready"}
	}
	status := kvServiceStatus{
		phase:          phase,
		infrastructure: infrastructureCondition(infrastructureReady),
		pools:          poolsCondition(poolsConverged),
		ready:          readyCondition,
		binding:        binding,
	}
	if applyErr != nil {
		status.phase = inferencev1alpha1.KVServicePhaseDegraded
		status.pools = kvServiceCondition{reason: "ApplyFailed", message: "KVPools were not fully materialized"}
	}
	result := ctrl.Result{}
	if service.Spec.Master.HighAvailability != nil {
		result.RequeueAfter = 5 * time.Second
	}
	return result, errors.Join(applyErr, reconciler.updateStatus(ctx, service, status))
}

// reconcileInfrastructure applies the master resources and maintains their requester configuration lifecycle.
func (reconciler *KVServiceReconciler) reconcileInfrastructure(ctx context.Context, service *inferencev1alpha1.KVService) (*inferencev1alpha1.KVServiceBinding, error) {
	resources, err := desiredKVMasterResources(service)
	if err != nil {
		return nil, err
	}
	pending, err := reconciler.reconcileMasterWorkloadMode(ctx, service, resources)
	if err != nil || pending {
		return nil, err
	}
	requesterName, err := reconciler.reconcileRequesterConfig(ctx, service, resources.requesterConfig)
	if err != nil {
		return nil, err
	}
	objects := []client.Object{resources.config}
	for _, kubeService := range resources.services {
		objects = append(objects, kubeService)
	}
	if resources.deployment != nil {
		objects = append(objects, resources.deployment)
	}
	if resources.statefulSet != nil {
		objects = append(objects, resources.statefulSet)
	}
	for _, object := range objects {
		if err := reconciler.applyOwned(ctx, service, object); err != nil {
			return nil, err
		}
	}
	if resources.statefulSet != nil {
		if err := reconciler.reconcileHAMasterUpdate(ctx, service, resources.statefulSet); err != nil {
			return nil, err
		}
	}
	if err := reconciler.removeStaleRequesterConfigs(ctx, service, requesterName); err != nil {
		return nil, err
	}
	if resources.pvc != nil {
		if err := reconciler.applyOwned(ctx, service, resources.pvc); err != nil {
			return nil, err
		}
	} else if service.Spec.Master.HighAvailability == nil {
		if err := reconciler.reconcileSnapshotRemoval(ctx, service); err != nil {
			return nil, err
		}
	}
	return desiredKVServiceBinding(requesterName, resources.connection), nil
}

// reconcileMasterWorkloadMode removes the superseded workload before changing its shared config.
// Mode switches are explicit cache-loss migrations and never run both Master modes concurrently.
func (reconciler *KVServiceReconciler) reconcileMasterWorkloadMode(ctx context.Context, service *inferencev1alpha1.KVService, desired kvMasterResources) (bool, error) {
	masterName, _, _, _ := kvMasterNames(service)
	if desired.statefulSet != nil {
		present, err := reconciler.deleteIfPresent(ctx, &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace}})
		return present, err
	}
	present, err := reconciler.deleteIfPresent(ctx, &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace}})
	if err != nil || present {
		return present, err
	}
	present, err = reconciler.deleteIfPresent(ctx, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: kvMasterHeadlessServiceName(service), Namespace: service.Namespace}})
	return present, err
}

// reconcileHAMasterUpdate replaces the observed standby before the leader for an OnDelete StatefulSet.
func (reconciler *KVServiceReconciler) reconcileHAMasterUpdate(ctx context.Context, service *inferencev1alpha1.KVService, desired *appsv1.StatefulSet) error {
	members, err := reconciler.observeHAMasterMembers(ctx, service)
	if err != nil || len(members) != 2 || !haMasterCohortReady(members) {
		return err
	}
	desiredRevision := desired.Spec.Template.Annotations[kvMasterConfigRevision]
	var outdatedLeader *corev1.Pod
	upToDateStandby := false
	for _, member := range members {
		if member.pod.Annotations[kvMasterConfigRevision] == desiredRevision {
			if member.health.Role == "standby" {
				upToDateStandby = true
			}
			continue
		}
		if member.health.Role == "standby" {
			return reconciler.Delete(ctx, member.pod, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &member.pod.UID})
		}
		if member.health.Role == "leader" {
			outdatedLeader = member.pod
		}
	}
	if outdatedLeader != nil && upToDateStandby {
		return reconciler.Delete(ctx, outdatedLeader, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &outdatedLeader.UID})
	}
	return nil
}

// reconcileRequesterConfig reuses a configuration with the same connection
// settings. Referenced configurations are created once and never updated in place.
// Querying resources instead of relying only on status avoids duplicate versions
// after a restart or a temporary loss of readiness.
func (reconciler *KVServiceReconciler) reconcileRequesterConfig(ctx context.Context, service *inferencev1alpha1.KVService, desired *corev1.ConfigMap) (string, error) {
	configs := new(corev1.ConfigMapList)
	if err := reconciler.List(ctx, configs, client.InNamespace(service.Namespace), client.MatchingLabels(desired.Labels)); err != nil {
		return "", err
	}
	var selected *corev1.ConfigMap
	for index := range configs.Items {
		config := &configs.Items[index]
		if !config.DeletionTimestamp.IsZero() || !metav1.IsControlledBy(config, service) || !reflect.DeepEqual(config.Data, desired.Data) || len(config.BinaryData) != 0 {
			continue
		}
		if service.Status.Binding != nil && config.Name == service.Status.Binding.ConfigMapName {
			return config.Name, nil
		}
		if selected == nil || config.CreationTimestamp.Before(&selected.CreationTimestamp) || (config.CreationTimestamp.Equal(&selected.CreationTimestamp) && config.Name < selected.Name) {
			selected = config
		}
	}
	if selected != nil {
		return selected.Name, nil
	}
	if err := controllerutil.SetControllerReference(service, desired, reconciler.Scheme()); err != nil {
		return "", err
	}
	if err := reconciler.Create(ctx, desired); err != nil {
		return "", fmt.Errorf("create requester configuration: %w", err)
	}
	return desired.Name, nil
}

// removeStaleRequesterConfigs deletes unreferenced requester ConfigMaps still owned by the KVService.
func (reconciler *KVServiceReconciler) removeStaleRequesterConfigs(ctx context.Context, service *inferencev1alpha1.KVService, currentName string) error {
	referenced := map[string]struct{}{currentName: {}}
	if service.Status.Binding != nil {
		referenced[service.Status.Binding.ConfigMapName] = struct{}{}
	}
	pools := new(inferencev1alpha1.ModelPoolList)
	if err := reconciler.List(ctx, pools, client.InNamespace(service.Namespace)); err != nil {
		return err
	}
	for index := range pools.Items {
		store := pools.Items[index].Spec.Template.KVCache
		if store != nil && store.MooncakeStore != nil && store.MooncakeStore.ManagedBinding != nil && store.MooncakeStore.ManagedBinding.UID == string(service.UID) {
			referenced[store.MooncakeStore.ManagedBinding.ConfigMapName] = struct{}{}
		}
	}
	groups := new(inferencev1alpha1.ModelGroupList)
	if err := reconciler.List(ctx, groups, client.InNamespace(service.Namespace)); err != nil {
		return err
	}
	for index := range groups.Items {
		store := groups.Items[index].Spec.KVRuntime
		if store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceUID == string(service.UID) {
			referenced[store.MooncakeStore.ConfigMapName] = struct{}{}
		}
	}

	configs := new(corev1.ConfigMapList)
	if err := reconciler.List(ctx, configs, client.InNamespace(service.Namespace), client.MatchingLabels{
		kvServiceLabel:                     kvLabelValue(service.Name),
		"inference.foretoken.io/component": "mooncake-requester",
	}); err != nil {
		return err
	}
	for index := range configs.Items {
		config := &configs.Items[index]
		_, retained := referenced[config.Name]
		if !retained && metav1.IsControlledBy(config, service) {
			if err := reconciler.Delete(ctx, config); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		}
	}
	return nil
}

// reconcileSnapshotRemoval releases or deletes the snapshot PVC after snapshot storage is removed.
func (reconciler *KVServiceReconciler) reconcileSnapshotRemoval(ctx context.Context, service *inferencev1alpha1.KVService) error {
	_, _, pvcName, _ := kvMasterNames(service)
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: service.Namespace}}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(pvc), pvc); err != nil {
		return client.IgnoreNotFound(err)
	}
	// Retention is recorded at creation, so removing snapshot cannot reinterpret
	// a previously retained PVC from the now-absent KVService spec. Retain
	// releases it; a later snapshot must use its UID-scoped PVC, never adopt it.
	if pvc.Annotations[snapshotRetentionAnnotation] == string(inferencev1alpha1.RetentionPolicyRetain) {
		return reconciler.releaseSnapshotPVC(ctx, service, pvc)
	}
	_, err := reconciler.deleteIfPresent(ctx, pvc)
	return err
}

// applyOwned creates or updates one KVService-owned resource while retaining allocated fields.
func (reconciler *KVServiceReconciler) applyOwned(ctx context.Context, owner *inferencev1alpha1.KVService, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	key := client.ObjectKeyFromObject(desired)
	err := reconciler.Get(ctx, key, current)
	missing := apierrors.IsNotFound(err)
	if err != nil && !missing {
		return err
	}
	if !missing && !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("%T %q is not controlled by KVService", current, current.GetName())
	}
	if err := controllerutil.SetControllerReference(owner, desired, reconciler.Scheme()); err != nil {
		return err
	}
	switch desired.(type) {
	case *appsv1.Deployment, *appsv1.StatefulSet:
		// Workload controllers own their revision metadata; server-side apply keeps
		// the KVService controller responsible only for the desired workload spec.
		return reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner("foretoken-kvservice"), client.ForceOwnership)
	}
	if missing {
		return reconciler.Create(ctx, desired)
	}
	if desiredPVC, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		currentPVC := current.(*corev1.PersistentVolumeClaim)
		preservePVCBindingAndMetadata(desiredPVC, currentPVC)
		if retention, recorded := currentPVC.Annotations[snapshotRetentionAnnotation]; recorded {
			desiredPVC.Annotations[snapshotRetentionAnnotation] = retention
		}
	}
	if desiredService, ok := desired.(*corev1.Service); ok {
		currentService := current.(*corev1.Service)
		desiredService.Spec.ClusterIP = currentService.Spec.ClusterIP
		desiredService.Spec.ClusterIPs = currentService.Spec.ClusterIPs
		desiredService.Spec.IPFamilies = currentService.Spec.IPFamilies
		desiredService.Spec.IPFamilyPolicy = currentService.Spec.IPFamilyPolicy
	}
	desired.SetResourceVersion(current.GetResourceVersion())
	return reconciler.Update(ctx, desired)
}

// reconcilePools converges the KVService storage-pool intent into owned KVPools.
func (reconciler *KVServiceReconciler) reconcilePools(ctx context.Context, service *inferencev1alpha1.KVService, owned []inferencev1alpha1.KVPool, desired []inferencev1alpha1.KVPoolSpec) error {
	byName := map[string]*inferencev1alpha1.KVPool{}
	for index := range owned {
		pool := &owned[index]
		byName[pool.Spec.PoolName] = pool
	}
	desiredNames := map[string]struct{}{}
	for _, desiredSpec := range desired {
		desiredNames[desiredSpec.PoolName] = struct{}{}
		pool := byName[desiredSpec.PoolName]
		if pool == nil {
			pool = &inferencev1alpha1.KVPool{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: service.Namespace,
					Name:      poolObjectName(service, desiredSpec.PoolName),
				},
				Spec: desiredSpec,
			}
			if err := controllerutil.SetControllerReference(service, pool, reconciler.Scheme()); err != nil {
				return err
			}
			if err := reconciler.Create(ctx, pool); err != nil {
				return fmt.Errorf("create KVPool %q: %w", desiredSpec.PoolName, err)
			}
			continue
		}
		if reflect.DeepEqual(pool.Spec, desiredSpec) {
			continue
		}
		if pool.Spec.KVServiceRef == desiredSpec.KVServiceRef && pool.Spec.PoolName == desiredSpec.PoolName && pool.Spec.Revision == desiredSpec.Revision && reflect.DeepEqual(pool.Spec.Template, desiredSpec.Template) {
			// Keep the Pool, Groups 0..N-1, and their PVCs intact while updating
			// mutable capacity or the resolved Master admin port.
			base := pool.DeepCopy()
			pool.Spec.DesiredGroups = desiredSpec.DesiredGroups
			pool.Spec.MasterAdminPort = desiredSpec.MasterAdminPort
			if err := reconciler.Patch(ctx, pool, client.MergeFrom(base)); err != nil {
				return fmt.Errorf("update KVPool %q: %w", pool.Name, err)
			}
			continue
		}
		// A template or revision change requires a replacement Pool because those
		// fields define immutable KVGroup specs.
		if err := reconciler.Delete(ctx, pool); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("replace KVPool %q: %w", pool.Name, err)
		}
		return nil
	}
	for index := range owned {
		pool := &owned[index]
		if _, keep := desiredNames[pool.Spec.PoolName]; !keep {
			if err := reconciler.Delete(ctx, pool); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		}
	}
	return nil
}

// normalizedKVPoolSpecs resolves each Pool once for both observation and writes.
func normalizedKVPoolSpecs(service *inferencev1alpha1.KVService) ([]inferencev1alpha1.KVPoolSpec, error) {
	pools := make([]inferencev1alpha1.KVPoolSpec, 0, len(service.Spec.StoragePools))
	for _, template := range service.Spec.StoragePools {
		pool, err := normalizedKVPoolSpec(service, template)
		if err != nil {
			return nil, fmt.Errorf("storage pool %q: %w", template.Name, err)
		}
		pools = append(pools, pool)
	}
	return pools, nil
}

// normalizedKVPoolSpec freezes client configuration and the resolved admin port for a KVPool.
// Capacity spellings resolve to byte counts before revision calculation.
func normalizedKVPoolSpec(service *inferencev1alpha1.KVService, template inferencev1alpha1.KVStoragePoolTemplate) (inferencev1alpha1.KVPoolSpec, error) {
	memoryBytes, err := resourcevalidation.ParsePositiveBytes("client.memoryCapacity", string(template.Client.MemoryCapacity))
	if err != nil {
		return inferencev1alpha1.KVPoolSpec{}, err
	}
	template.Client.MemoryCapacity = inferencev1alpha1.ResourceQuantity(strconv.FormatInt(memoryBytes, 10))
	if template.Client.Disk != nil {
		diskBytes, err := resourcevalidation.ParsePositiveBytes("client.disk.size", string(template.Client.Disk.Size))
		if err != nil {
			return inferencev1alpha1.KVPoolSpec{}, err
		}
		disk := *template.Client.Disk
		disk.Size = inferencev1alpha1.ResourceQuantity(strconv.FormatInt(diskBytes, 10))
		template.Client.Disk = &disk
	}
	if template.Client.Port == 0 {
		template.Client.Port = 50052
	}
	if template.Client.Protocol == "rdma" && template.Client.RDMAResourceCount == 0 {
		template.Client.RDMAResourceCount = 1
	}
	if template.Client.StorageRegistration != nil {
		registration := *template.Client.StorageRegistration
		if registration.Port == 0 {
			registration.Port = inferencev1alpha1.DefaultStorageRegistrationPort
		}
		template.Client.StorageRegistration = &registration
	}
	normalized := inferencev1alpha1.NormalizedKVPoolTemplate{Client: template.Client, NodeSelector: template.NodeSelector}
	connection, err := resolveKVMasterConnection(service)
	if err != nil {
		return inferencev1alpha1.KVPoolSpec{}, err
	}
	adminPort := int32(0)
	if template.Client.StorageRegistration != nil && template.Client.StorageRegistration.Enabled {
		_, _, adminPort = masterPorts(service.Spec.Master)
	}
	return inferencev1alpha1.KVPoolSpec{
		KVServiceRef:    inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
		PoolName:        template.Name,
		Revision:        kvPoolRevision(normalized, connection.ServerAddress, adminPort),
		MasterAdminPort: adminPort,
		DesiredGroups:   template.Replicas,
		Template:        normalized,
	}, nil
}

func poolObjectName(service *inferencev1alpha1.KVService, poolName string) string {
	// Include the full source pool name in the hash. Truncation alone can make
	// distinct long pool names collide and must never choose the wrong capacity.
	return kvChildName(service.Name+"-"+poolName, string(service.UID)+":"+poolName)
}

// ownedPools returns KVPools whose reference and controller owner both identify the KVService.
func (reconciler *KVServiceReconciler) ownedPools(ctx context.Context, service *inferencev1alpha1.KVService) ([]inferencev1alpha1.KVPool, error) {
	var list inferencev1alpha1.KVPoolList
	if err := reconciler.List(ctx, &list, client.InNamespace(service.Namespace)); err != nil {
		return nil, err
	}
	owned := make([]inferencev1alpha1.KVPool, 0, len(list.Items))
	for index := range list.Items {
		pool := &list.Items[index]
		ref := pool.Spec.KVServiceRef.Name == service.Name && pool.Spec.KVServiceRef.UID == string(service.UID)
		controlled := metav1.IsControlledBy(pool, service)
		if ref != controlled {
			return nil, fmt.Errorf("KVPool %q has inconsistent KVService ownership", pool.Name)
		}
		if ref {
			owned = append(owned, *pool)
		}
	}
	return owned, nil
}

// infrastructureReady reads the persisted master Deployment availability.
func (reconciler *KVServiceReconciler) infrastructureReady(ctx context.Context, service *inferencev1alpha1.KVService) (bool, error) {
	master, _, _, _ := kvMasterNames(service)
	if service.Spec.Master.HighAvailability == nil {
		deployment := new(appsv1.Deployment)
		if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: master}, deployment); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		return frontendDeploymentAvailable(deployment), nil
	}
	members, err := reconciler.observeHAMasterMembers(ctx, service)
	if err != nil {
		return false, err
	}
	return haMasterCohortReady(members), nil
}

type mooncakeMasterHealth struct {
	Status        string  `json:"status"`
	Role          string  `json:"role"`
	HAState       string  `json:"ha_state"`
	ServiceReady  bool    `json:"service_ready"`
	LeaderAddress *string `json:"leader_address,omitempty"`
	ViewVersion   *uint64 `json:"view_version,omitempty"`
}

type haMasterMember struct {
	pod    *corev1.Pod
	health mooncakeMasterHealth
}

// observeHAMasterMembers reads each native admin endpoint without deriving a second leader state.
func (reconciler *KVServiceReconciler) observeHAMasterMembers(ctx context.Context, service *inferencev1alpha1.KVService) ([]haMasterMember, error) {
	masterName, _, _, _ := kvMasterNames(service)
	master := new(appsv1.StatefulSet)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: masterName}, master); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(master, service) {
		return nil, fmt.Errorf("Master StatefulSet %q is not owned by this KVService", master.Name)
	}
	pods := new(corev1.PodList)
	if err := reconciler.List(ctx, pods, client.InNamespace(service.Namespace), client.MatchingLabels{kvServiceLabel: kvLabelValue(service.Name), "inference.foretoken.io/component": "mooncake-master", kvMasterModeLabel: "ha"}); err != nil {
		return nil, err
	}
	_, _, metricsPort := masterPorts(service.Spec.Master)
	members := make([]haMasterMember, 0, len(pods.Items))
	for index := range pods.Items {
		pod := &pods.Items[index]
		if !metav1.IsControlledBy(pod, master) || !pod.DeletionTimestamp.IsZero() || pod.Status.PodIP == "" {
			continue
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(metricsPort)))+"/health", nil)
		if err != nil {
			return nil, err
		}
		response, err := reconciler.HTTPClient.Do(request)
		if err != nil {
			continue
		}
		var health mooncakeMasterHealth
		decodeErr := json.NewDecoder(response.Body).Decode(&health)
		response.Body.Close()
		if response.StatusCode != http.StatusOK || decodeErr != nil {
			continue
		}
		members = append(members, haMasterMember{pod: pod, health: health})
	}
	return members, nil
}

// haMasterCohortReady requires one serving leader and one caught-up standby in the same native view.
func haMasterCohortReady(members []haMasterMember) bool {
	if len(members) != 2 {
		return false
	}
	leaders, standbys := 0, 0
	var leaderAddress string
	var view uint64
	for _, member := range members {
		switch {
		case member.health.Role == "leader" && member.health.HAState == "serving" && member.health.ServiceReady && member.health.LeaderAddress != nil && member.health.ViewVersion != nil:
			leaders++
			leaderAddress, view = *member.health.LeaderAddress, *member.health.ViewVersion
		case member.health.Role == "standby" && member.health.HAState == "standby" && !member.health.ServiceReady && member.health.LeaderAddress != nil && member.health.ViewVersion != nil:
			standbys++
		default:
			return false
		}
	}
	if leaders != 1 || standbys != 1 {
		return false
	}
	for _, member := range members {
		if member.health.LeaderAddress == nil || member.health.ViewVersion == nil || *member.health.LeaderAddress != leaderAddress || *member.health.ViewVersion != view {
			return false
		}
	}
	return true
}

// kvPoolState validates the complete Pool observation before writes and reports
// convergence separately from compatible client Kubernetes availability.
func kvPoolState(pools []inferencev1alpha1.KVPool, desiredPools []inferencev1alpha1.KVPoolSpec) (bool, bool, error) {
	byName := make(map[string]*inferencev1alpha1.KVPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		if byName[pool.Spec.PoolName] != nil {
			return false, false, fmt.Errorf("KVService owns duplicate KVPools for poolName %q", pool.Spec.PoolName)
		}
		byName[pool.Spec.PoolName] = pool
	}
	converged, available := len(pools) == len(desiredPools), false
	for _, desired := range desiredPools {
		pool := byName[desired.PoolName]
		if pool == nil || !pool.DeletionTimestamp.IsZero() || pool.Spec.KVServiceRef != desired.KVServiceRef || pool.Spec.Revision != desired.Revision || pool.Spec.MasterAdminPort != desired.MasterAdminPort || !reflect.DeepEqual(pool.Spec.Template, desired.Template) {
			converged = false
			continue
		}
		// Pool templates are immutable; only capacity changes. A previous
		// availability result remains valid when the desired count grows.
		// Pools with changed templates or transports were excluded above, and a
		// zero-target Pool provides no capacity.
		condition := meta.FindStatusCondition(pool.Status.Conditions, conditionReady)
		if desired.DesiredGroups > 0 && condition != nil && condition.Status == metav1.ConditionTrue {
			available = true
		}
		materialized := meta.FindStatusCondition(pool.Status.Conditions, conditionGroupsMaterialized)
		settled := pool.Status.Phase == inferencev1alpha1.KVPoolPhaseReady || (desired.DesiredGroups == 0 && pool.Status.Phase == inferencev1alpha1.KVPoolPhasePending)
		if pool.Spec.DesiredGroups != desired.DesiredGroups || pool.Status.ObservedGeneration != pool.Generation || materialized == nil || materialized.Status != metav1.ConditionTrue || materialized.ObservedGeneration != pool.Generation || !settled {
			converged = false
		}
	}
	return converged, available, nil
}

func (reconciler *KVServiceReconciler) reconcileDelete(ctx context.Context, service *inferencev1alpha1.KVService) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(service, kvServiceFinalizer) {
		return ctrl.Result{}, nil
	}
	if blocked, err := reconciler.hasConsumers(ctx, service); err != nil {
		return ctrl.Result{}, err
	} else if blocked {
		_ = reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseTerminating,
			infrastructure: kvServiceCondition{reason: "Deleting", message: "KVService is deleting"},
			pools:          kvServiceCondition{reason: "ReferencesPresent", message: "KVService still has consumers"},
			ready:          kvServiceCondition{reason: "ReferencesResolved", message: "Wait for ModelService, ModelPool, and ModelGroup consumers to disappear"},
		})
		return ctrl.Result{Requeue: true}, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	for index := range pools {
		if err := reconciler.Delete(ctx, &pools[index]); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
	}
	if len(pools) > 0 {
		return ctrl.Result{Requeue: true}, nil
	}
	// No provider drain is asserted in v1alpha1. Remove infrastructure only after
	// the controller-owned Pools are gone, and wait for its deletion to converge.
	pending := false
	masterName, configName, pvcName, serviceName := kvMasterNames(service)
	for _, object := range []client.Object{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace}}, &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace}}, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: serviceName, Namespace: service.Namespace}}, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: kvMasterHeadlessServiceName(service), Namespace: service.Namespace}}, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: configName, Namespace: service.Namespace}}} {
		present, err := reconciler.deleteIfPresent(ctx, object)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || present
	}
	// The PVC annotation is immutable lifecycle intent recorded at creation; it
	// remains authoritative even if the current spec was edited before deletion.
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: service.Namespace}}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(pvc), pvc); err == nil {
		if pvc.Annotations[snapshotRetentionAnnotation] == string(inferencev1alpha1.RetentionPolicyRetain) {
			if err := reconciler.releaseSnapshotPVC(ctx, service, pvc); err != nil {
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
	base := service.DeepCopy()
	controllerutil.RemoveFinalizer(service, kvServiceFinalizer)
	if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *KVServiceReconciler) deleteIfPresent(ctx context.Context, object client.Object) (bool, error) {
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

func (reconciler *KVServiceReconciler) releaseSnapshotPVC(ctx context.Context, service *inferencev1alpha1.KVService, pvc *corev1.PersistentVolumeClaim) error {
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(pvc), pvc); err != nil {
		return client.IgnoreNotFound(err)
	}
	base := pvc.DeepCopy()
	owners := pvc.OwnerReferences[:0]
	for _, owner := range pvc.OwnerReferences {
		if owner.UID != service.UID {
			owners = append(owners, owner)
		}
	}
	pvc.OwnerReferences = owners
	if reflect.DeepEqual(base.OwnerReferences, pvc.OwnerReferences) {
		return nil
	}
	return reconciler.Patch(ctx, pvc, client.MergeFrom(base))
}

// Status projection keeps resource reconciliation separate from condition details.
func (reconciler *KVServiceReconciler) updateStatus(ctx context.Context, service *inferencev1alpha1.KVService, desired kvServiceStatus) error {
	base := service.DeepCopy()
	service.Status.ObservedGeneration = service.Generation
	service.Status.Phase = desired.phase
	if desired.ready.ready {
		service.Status.Binding = desired.binding
	} else {
		service.Status.Binding = nil
	}
	setKVServiceCondition(service, conditionInfrastructureReady, desired.infrastructure)
	setKVServiceCondition(service, conditionKVPoolsMaterialized, desired.pools)
	setKVServiceCondition(service, conditionReady, desired.ready)
	if reflect.DeepEqual(base.Status, service.Status) {
		return nil
	}
	return reconciler.Status().Patch(ctx, service, client.MergeFrom(base))
}

// desiredKVServiceBinding projects the selected configuration version to model
// consumers; changing the replica count does not change that version.
func desiredKVServiceBinding(requesterName string, connection kvMasterConnection) *inferencev1alpha1.KVServiceBinding {
	return &inferencev1alpha1.KVServiceBinding{
		Revision:       kvPoolRevision(inferencev1alpha1.NormalizedKVPoolTemplate{}, requesterName, 0),
		ConfigMapName:  requesterName,
		ConfigMapKey:   requesterConfigKey,
		MasterEndpoint: connection.ServerAddress,
		ClusterID:      connection.ClusterID,
		PythonHashSeed: "0",
	}
}

func setKVServiceCondition(service *inferencev1alpha1.KVService, conditionType string, desired kvServiceCondition) {
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{
		Type:               conditionType,
		Status:             conditionStatus(desired.ready),
		Reason:             desired.reason,
		Message:            desired.message,
		ObservedGeneration: service.Generation,
	})
}

func infrastructureCondition(ready bool) kvServiceCondition {
	if ready {
		return kvServiceCondition{ready: true, reason: "Available", message: "Master infrastructure is available"}
	}
	return kvServiceCondition{reason: "InfrastructureNotAvailable", message: "Master infrastructure is not available"}
}

func poolsCondition(ready bool) kvServiceCondition {
	if ready {
		return kvServiceCondition{ready: true, reason: "Applied", message: "All KVPools were materialized"}
	}
	return kvServiceCondition{reason: "Reconciling", message: "KVPools are not fully materialized"}
}

// hasConsumers reports whether live model resources still bind the KVService.
func (reconciler *KVServiceReconciler) hasConsumers(ctx context.Context, service *inferencev1alpha1.KVService) (bool, error) {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(service.Namespace)); err != nil {
		return false, err
	}
	for i := range services.Items {
		if modelServiceReferencesKV(&services.Items[i], service.Name) {
			return true, nil
		}
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		return false, err
	}
	for i := range pools.Items {
		if binding := pools.Items[i].Spec.Template.KVCache; binding != nil && binding.MooncakeStore != nil && binding.MooncakeStore.ManagedBinding != nil && binding.MooncakeStore.ManagedBinding.UID == string(service.UID) {
			return true, nil
		}
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, err
	}
	for i := range groups.Items {
		if store := groups.Items[i].Spec.KVRuntime; store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceUID == string(service.UID) {
			return true, nil
		}
	}
	return false, nil
}

func modelServiceReferencesKV(service *inferencev1alpha1.ModelService, name string) bool {
	if store := service.Spec.KVCache; store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceRef != nil && store.MooncakeStore.KVServiceRef.Name == name {
		return true
	}
	for _, pool := range service.Spec.ModelPools {
		if store := pool.KVCache; store != nil && store.MooncakeStore != nil && store.MooncakeStore.KVServiceRef != nil && store.MooncakeStore.KVServiceRef.Name == name {
			return true
		}
	}
	return false
}
