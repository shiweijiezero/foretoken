// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles KVService into its Master infrastructure and controller-owned KVPools.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
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

type KVServiceReconciler struct{ client.Client }

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
}

// SetupWithManager registers KVService reconciliation for master infrastructure and KVPools.
func (reconciler *KVServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.KVService{}).
		Owns(&inferencev1alpha1.KVPool{}).
		Owns(&appsv1.Deployment{}).
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
	if err := reconciler.reconcileInfrastructure(ctx, service); err != nil {
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseDegraded,
			infrastructure: kvServiceCondition{reason: "ApplyFailed", message: "Master infrastructure was not fully materialized"},
			pools:          kvServiceCondition{reason: "ApplyFailed", message: "KVPools were not fully materialized"},
			ready:          kvServiceCondition{reason: "InfrastructureNotReady", message: "Master infrastructure is not ready"},
		}))
	}
	if err := reconciler.reconcilePools(ctx, service); err != nil {
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, service, kvServiceStatus{
			phase:          inferencev1alpha1.KVServicePhaseDegraded,
			infrastructure: kvServiceCondition{reason: "ApplyFailed", message: "Master infrastructure was not fully materialized"},
			pools:          kvServiceCondition{reason: "ApplyFailed", message: "KVPools were not fully materialized"},
			ready:          kvServiceCondition{reason: "PoolsNotReady", message: "KVPools are not ready"},
		}))
	}
	infrastructureReady, err := reconciler.infrastructureReady(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	poolsMaterialized, poolsReady, err := reconciler.poolState(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	ready := infrastructureReady && poolsMaterialized && poolsReady
	phase := inferencev1alpha1.KVServicePhaseProgressing
	if ready {
		phase = inferencev1alpha1.KVServicePhaseReady
	}
	readyCondition := kvServiceCondition{reason: "DependenciesNotReady", message: "Master infrastructure or KVPools are not ready"}
	if ready {
		readyCondition = kvServiceCondition{ready: true, reason: "Ready", message: "Master infrastructure is available and all KVPools are materialized"}
	}
	return ctrl.Result{}, reconciler.updateStatus(ctx, service, kvServiceStatus{
		phase:          phase,
		infrastructure: infrastructureCondition(infrastructureReady),
		pools:          poolsCondition(poolsMaterialized && poolsReady),
		ready:          readyCondition,
	})
}

// reconcileInfrastructure applies the master resources and maintains their requester configuration lifecycle.
func (reconciler *KVServiceReconciler) reconcileInfrastructure(ctx context.Context, service *inferencev1alpha1.KVService) error {
	config, requesterConfig, deployment, kubeService, pvc, err := desiredKVMasterResources(service)
	if err != nil {
		return err
	}
	for _, object := range []client.Object{config, requesterConfig, deployment, kubeService} {
		if err := reconciler.applyOwned(ctx, service, object); err != nil {
			return err
		}
	}
	if err := reconciler.removeStaleRequesterConfigs(ctx, service, requesterConfig.Name); err != nil {
		return err
	}
	if pvc != nil {
		if err := reconciler.applyOwned(ctx, service, pvc); err != nil {
			return err
		}
	} else if err := reconciler.reconcileSnapshotRemoval(ctx, service); err != nil {
		return err
	}
	return nil
}

// removeStaleRequesterConfigs deletes unreferenced requester ConfigMaps still owned by the KVService.
func (reconciler *KVServiceReconciler) removeStaleRequesterConfigs(ctx context.Context, service *inferencev1alpha1.KVService, currentName string) error {
	referenced := map[string]struct{}{currentName: {}}
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
	if apierrors.IsNotFound(err) {
		if err := controllerutil.SetControllerReference(owner, desired, reconciler.Scheme()); err != nil {
			return err
		}
		return reconciler.Create(ctx, desired)
	}
	if err != nil {
		return err
	}
	if !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("%T %q is not controlled by KVService", current, current.GetName())
	}
	if desiredPVC, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		currentPVC := current.(*corev1.PersistentVolumeClaim)
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
	if err := controllerutil.SetControllerReference(owner, desired, reconciler.Scheme()); err != nil {
		return err
	}
	return reconciler.Update(ctx, desired)
}

// reconcilePools converges the KVService storage-pool intent into owned KVPools.
func (reconciler *KVServiceReconciler) reconcilePools(ctx context.Context, service *inferencev1alpha1.KVService) error {
	owned, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return err
	}
	byName := map[string]*inferencev1alpha1.KVPool{}
	for index := range owned {
		pool := &owned[index]
		if byName[pool.Spec.PoolName] != nil {
			return fmt.Errorf("KVService owns duplicate KVPools for poolName %q", pool.Spec.PoolName)
		}
		byName[pool.Spec.PoolName] = pool
	}
	desiredNames := map[string]struct{}{}
	for _, template := range service.Spec.StoragePools {
		desiredNames[template.Name] = struct{}{}
		pool := byName[template.Name]
		if pool == nil {
			pool = &inferencev1alpha1.KVPool{ObjectMeta: metav1.ObjectMeta{Namespace: service.Namespace, Name: poolObjectName(service, template.Name)}}
			if err := controllerutil.SetControllerReference(service, pool, reconciler.Scheme()); err != nil {
				return err
			}
			pool.Spec = normalizedKVPoolSpec(service, template)
			if err := reconciler.Create(ctx, pool); err != nil {
				return fmt.Errorf("create KVPool %q: %w", template.Name, err)
			}
			continue
		}
		desiredSpec := normalizedKVPoolSpec(service, template)
		if reflect.DeepEqual(pool.Spec, desiredSpec) {
			continue
		}
		if pool.Spec.KVServiceRef == desiredSpec.KVServiceRef && pool.Spec.PoolName == desiredSpec.PoolName && pool.Spec.Revision == desiredSpec.Revision && reflect.DeepEqual(pool.Spec.Template, desiredSpec.Template) {
			// Scaling is the sole mutable KVPool field. Keep the Pool, Groups 0..N-1,
			// and their PVCs intact; KVPool reconciles only the ordinal delta.
			base := pool.DeepCopy()
			pool.Spec.DesiredGroups = desiredSpec.DesiredGroups
			if err := reconciler.Patch(ctx, pool, client.MergeFrom(base)); err != nil {
				return fmt.Errorf("scale KVPool %q: %w", pool.Name, err)
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

func normalizedKVPoolSpec(service *inferencev1alpha1.KVService, template inferencev1alpha1.KVStoragePoolTemplate) inferencev1alpha1.KVPoolSpec {
	if template.Replicas == 0 { /* explicit zero is preserved; API defaulting supplies omitted value */
	}
	if template.Client.Port == 0 {
		template.Client.Port = 50052
	}
	if template.Client.RDMAResourceCount == 0 {
		template.Client.RDMAResourceCount = 1
	}
	normalized := inferencev1alpha1.NormalizedKVPoolTemplate{Client: template.Client, NodeSelector: template.NodeSelector}
	_, _, _, masterService := kvMasterNames(service)
	rpcPort, _, _ := masterPorts(service.Spec.Master)
	return inferencev1alpha1.KVPoolSpec{KVServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)}, PoolName: template.Name, Revision: kvPoolRevision(normalized, masterService, rpcPort), DesiredGroups: template.Replicas, Template: normalized}
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
	deployment := new(appsv1.Deployment)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: master}, deployment); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return frontendDeploymentAvailable(deployment), nil
}

// poolState aggregates materialization and readiness across the KVService KVPools.
func (reconciler *KVServiceReconciler) poolState(ctx context.Context, service *inferencev1alpha1.KVService) (bool, bool, error) {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, false, err
	}
	if len(pools) != len(service.Spec.StoragePools) {
		return false, false, nil
	}
	byName := make(map[string]*inferencev1alpha1.KVPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		if !pool.DeletionTimestamp.IsZero() || byName[pool.Spec.PoolName] != nil {
			return false, false, nil
		}
		byName[pool.Spec.PoolName] = pool
	}
	ready := true
	for _, template := range service.Spec.StoragePools {
		pool := byName[template.Name]
		if pool == nil || !reflect.DeepEqual(pool.Spec, normalizedKVPoolSpec(service, template)) {
			return false, false, nil
		}
		condition := meta.FindStatusCondition(pool.Status.Conditions, conditionReady)
		if pool.Status.ObservedGeneration != pool.Generation || condition == nil || condition.Status != metav1.ConditionTrue || condition.ObservedGeneration != pool.Generation {
			ready = false
		}
	}
	return true, ready, nil
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
	for _, object := range []client.Object{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace}}, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: serviceName, Namespace: service.Namespace}}, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: configName, Namespace: service.Namespace}}} {
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
		service.Status.Binding = desiredKVServiceBinding(service)
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

func desiredKVServiceBinding(service *inferencev1alpha1.KVService) *inferencev1alpha1.KVServiceBinding {
	_, _, _, masterService := kvMasterNames(service)
	// Requester ConfigMaps are UID/generation-scoped, so a resolved consumer cannot
	// accidentally attach to a same-name KVService recreated after deletion.
	requesterName := kvChildName(service.Name+"-requester-config", string(service.UID)+":"+fmt.Sprint(service.Generation))
	rpcPort, _, _ := masterPorts(service.Spec.Master)
	return &inferencev1alpha1.KVServiceBinding{
		Revision:       kvPoolRevision(inferencev1alpha1.NormalizedKVPoolTemplate{}, requesterName, rpcPort),
		ConfigMapName:  requesterName,
		ConfigMapKey:   requesterConfigKey,
		MasterEndpoint: fmt.Sprintf("%s.%s.svc:%d", masterService, service.Namespace, rpcPort),
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
		return kvServiceCondition{ready: true, reason: "Available", message: "Master Deployment is available"}
	}
	return kvServiceCondition{reason: "DeploymentNotAvailable", message: "Master Deployment is not available"}
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
