// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles KVPool into immutable revisioned Mooncake client Groups.

package controllers

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strconv"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const kvPoolFinalizer = "inference.foretoken.io/kvpool-protection"

type KVPoolReconciler struct{ client.Client }

// SetupWithManager registers KVPool reconciliation and KVGroup ownership watches.
func (reconciler *KVPoolReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.KVPool{}).
		Owns(&inferencev1alpha1.KVGroup{}).
		Complete(reconciler)
}

// Reconcile converges a KVPool into its requested immutable KVGroup revision.
func (reconciler *KVPoolReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	pool := new(inferencev1alpha1.KVPool)
	if err := reconciler.Get(ctx, request.NamespacedName, pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !pool.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, pool)
	}
	service := new(inferencev1alpha1.KVService)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.KVServiceRef.Name}, service); err != nil {
		return ctrl.Result{}, fmt.Errorf("get owning KVService: %w", err)
	}
	if pool.Spec.KVServiceRef.UID != string(service.UID) || !metav1.IsControlledBy(pool, service) {
		return ctrl.Result{}, fmt.Errorf("KVPool %q is not owned by its referenced KVService", pool.Name)
	}
	if !controllerutil.ContainsFinalizer(pool, kvPoolFinalizer) {
		base := pool.DeepCopy()
		controllerutil.AddFinalizer(pool, kvPoolFinalizer)
		if err := reconciler.Patch(ctx, pool, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}
	groups, err := reconciler.reconcileGroups(ctx, pool, service)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, pool, inferencev1alpha1.KVPoolPhaseDegraded, false, groups.ready, "ApplyFailed", "KVGroups were not fully materialized")
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	phase := inferencev1alpha1.KVPoolPhaseProgressing
	if groups.converged {
		phase = inferencev1alpha1.KVPoolPhaseReady
	}
	if pool.Spec.DesiredGroups == 0 {
		phase = inferencev1alpha1.KVPoolPhasePending
	}
	return ctrl.Result{}, reconciler.updateStatus(ctx, pool, phase, groups.materialized, groups.ready, groups.reason, groups.message)
}

type kvGroupState struct {
	materialized, ready, converged bool
	reason, message                string
}

// reconcileGroups 先观察兼容实例，再按删除、创建顺序收敛容量；写入错误携带现存可用状态返回。
func (reconciler *KVPoolReconciler) reconcileGroups(ctx context.Context, pool *inferencev1alpha1.KVPool, service *inferencev1alpha1.KVService) (kvGroupState, error) {
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return kvGroupState{}, err
	}
	desired, err := desiredKVGroupSpec(pool, service)
	if err != nil {
		return kvGroupState{}, err
	}
	current := make(map[int32]*inferencev1alpha1.KVGroup, len(groups))
	var retiring []*inferencev1alpha1.KVGroup
	readyCount := int32(0)
	materialized := true
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision != desired.Revision || group.Spec.Ordinal >= pool.Spec.DesiredGroups {
			retiring = append(retiring, group)
			materialized = false
			continue
		}
		if current[group.Spec.Ordinal] != nil {
			return kvGroupState{}, fmt.Errorf("KVPool owns duplicate KVGroups for ordinal %d", group.Spec.Ordinal)
		}
		spec := desired
		spec.Ordinal = group.Spec.Ordinal
		if !reflect.DeepEqual(group.Spec, spec) {
			return kvGroupState{}, fmt.Errorf("KVGroup %q has an unexpected immutable spec", group.Name)
		}
		current[group.Spec.Ordinal] = group
		if !group.DeletionTimestamp.IsZero() {
			materialized = false
		}
		if kvGroupReady(group) {
			readyCount++
		}
	}
	// 先观察保留实例，再增删容量；写入失败不抹掉已确认的可用性，观测错误不推测 Ready。
	state := kvGroupState{ready: readyCount > 0}
	for _, group := range retiring {
		if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
			return state, err
		}
	}
	pending := len(retiring) > 0
	if !pending {
		for ordinal := int32(0); ordinal < pool.Spec.DesiredGroups; ordinal++ {
			if current[ordinal] != nil {
				continue
			}
			spec := desired
			spec.Ordinal = ordinal
			group := &inferencev1alpha1.KVGroup{ObjectMeta: metav1.ObjectMeta{Namespace: pool.Namespace, Name: kvGroupObjectName(pool, spec.Revision, ordinal)}, Spec: spec}
			if err := controllerutil.SetControllerReference(pool, group, reconciler.Scheme()); err != nil {
				return state, err
			}
			if err := reconciler.Create(ctx, group); err != nil {
				return state, fmt.Errorf("create KVGroup ordinal %d: %w", ordinal, err)
			}
		}
	}
	state.materialized = materialized
	state.converged = materialized && readyCount == pool.Spec.DesiredGroups
	state.reason, state.message = "Applied", "All requested KVGroups were materialized"
	if pending {
		state.reason, state.message = "ReplacementPending", "Superseded or excess KVGroups are being removed"
	} else if pool.Spec.DesiredGroups == 0 {
		state.message = "KVPool has no requested client capacity"
	}
	return state, nil
}

// desiredKVGroupSpec 为 Pool 解析共同的客户端配置与 Master 地址；调用方为每个实例设置 ordinal。
func desiredKVGroupSpec(pool *inferencev1alpha1.KVPool, service *inferencev1alpha1.KVService) (inferencev1alpha1.KVGroupSpec, error) {
	client := pool.Spec.Template.Client
	if client.Disk == nil {
		return inferencev1alpha1.KVGroupSpec{}, fmt.Errorf("KVPool %q requires disk for standalone Store offload", pool.Name)
	}
	if client.Protocol == "rdma" && client.RDMAResourceName == "" {
		return inferencev1alpha1.KVGroupSpec{}, fmt.Errorf("KVPool %q requires rdmaResourceName", pool.Name)
	}
	_, _, _, masterService := kvMasterNames(service)
	rpc, _, _ := masterPorts(service.Spec.Master)
	revision := pool.Spec.Revision
	retention := client.Disk.RetentionPolicy
	if retention == "" {
		retention = inferencev1alpha1.RetentionPolicyDelete
	}
	return inferencev1alpha1.KVGroupSpec{KVPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)}, Revision: revision, MasterServiceDNS: fmt.Sprintf("%s.%s.svc.cluster.local", masterService, pool.Namespace), MasterRPCPort: rpc, Client: inferencev1alpha1.KVGroupClientConfig{Image: client.Image, Protocol: client.Protocol, Port: client.Port, Resources: client.Resources, RDMAResourceName: client.RDMAResourceName, RDMAResourceCount: client.RDMAResourceCount, MemoryCapacityBytes: client.MemoryCapacity, Disk: inferencev1alpha1.KVGroupDisk{StorageClassName: client.Disk.StorageClassName, Size: client.Disk.Size, RetentionPolicy: retention}, NodeSelector: pool.Spec.Template.NodeSelector}, Timeouts: service.Spec.Timeouts}, nil
}

func kvPoolRevision(template inferencev1alpha1.NormalizedKVPoolTemplate, masterService string, rpcPort int32) string {
	input, err := json.Marshal(struct {
		Template inferencev1alpha1.NormalizedKVPoolTemplate
		Master   string
		Port     int32
	}{template, masterService, rpcPort})
	if err != nil {
		panic(err)
	}
	return fmt.Sprintf("%x", sha256.Sum256(input))[:12]
}

func kvGroupObjectName(pool *inferencev1alpha1.KVPool, revision string, ordinal int32) string {
	return kvChildName(pool.Name+"-"+revision+"-"+strconv.Itoa(int(ordinal)), string(pool.UID)+":"+revision+":"+strconv.Itoa(int(ordinal)))
}

// ownedGroups returns KVGroups whose reference and controller owner both identify the KVPool.
func (reconciler *KVPoolReconciler) ownedGroups(ctx context.Context, pool *inferencev1alpha1.KVPool) ([]inferencev1alpha1.KVGroup, error) {
	var list inferencev1alpha1.KVGroupList
	if err := reconciler.List(ctx, &list, client.InNamespace(pool.Namespace)); err != nil {
		return nil, err
	}
	owned := make([]inferencev1alpha1.KVGroup, 0, len(list.Items))
	for index := range list.Items {
		group := &list.Items[index]
		ref := group.Spec.KVPoolRef.Name == pool.Name && group.Spec.KVPoolRef.UID == string(pool.UID)
		controlled := metav1.IsControlledBy(group, pool)
		if ref != controlled {
			return nil, fmt.Errorf("KVGroup %q has inconsistent KVPool ownership", group.Name)
		}
		if ref {
			owned = append(owned, *group)
		}
	}
	return owned, nil
}

func kvGroupReady(group *inferencev1alpha1.KVGroup) bool {
	if group == nil || !group.DeletionTimestamp.IsZero() || group.Status.ObservedGeneration != group.Generation {
		return false
	}
	condition := meta.FindStatusCondition(group.Status.Conditions, conditionReady)
	return condition != nil && condition.Status == metav1.ConditionTrue && condition.ObservedGeneration == group.Generation
}

// reconcileDelete removes owned KVGroups before releasing the KVPool finalizer.
func (reconciler *KVPoolReconciler) reconcileDelete(ctx context.Context, pool *inferencev1alpha1.KVPool) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(pool, kvPoolFinalizer) {
		return ctrl.Result{}, nil
	}
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	for index := range groups {
		if err := reconciler.Delete(ctx, &groups[index]); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
	}
	if len(groups) > 0 {
		return ctrl.Result{Requeue: true}, nil
	}
	base := pool.DeepCopy()
	controllerutil.RemoveFinalizer(pool, kvPoolFinalizer)
	return ctrl.Result{}, reconciler.Patch(ctx, pool, client.MergeFrom(base))
}

// updateStatus publishes KVGroup materialization and readiness for the KVPool.
func (reconciler *KVPoolReconciler) updateStatus(ctx context.Context, pool *inferencev1alpha1.KVPool, phase inferencev1alpha1.KVPoolPhase, materialized, ready bool, reason, message string) error {
	base := pool.DeepCopy()
	pool.Status.ObservedGeneration = pool.Generation
	pool.Status.Phase = phase
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionGroupsMaterialized, Status: conditionStatus(materialized), Reason: reason, Message: message, ObservedGeneration: pool.Generation})
	readyReason, readyMessage := "ClientInfrastructureNotReady", "No requested Mooncake client workload is Kubernetes-ready"
	if ready {
		readyReason, readyMessage = "ClientInfrastructureReady", "At least one requested Mooncake client workload is Kubernetes-ready"
	}
	if pool.Spec.DesiredGroups == 0 {
		readyReason, readyMessage = "ScaledToZero", "KVPool has no requested client capacity"
	}
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionReady, Status: conditionStatus(ready), Reason: readyReason, Message: readyMessage, ObservedGeneration: pool.Generation})
	if reflect.DeepEqual(base.Status, pool.Status) {
		return nil
	}
	return reconciler.Status().Patch(ctx, pool, client.MergeFrom(base))
}
