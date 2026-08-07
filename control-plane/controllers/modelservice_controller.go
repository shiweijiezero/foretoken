// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Reconciles ModelService intent into controller-owned ModelPool resources.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	modelServiceFinalizer      = "inference.foretoken.io/modelservice-protection"
	conditionIntentCompiled    = "IntentCompiled"
	conditionPoolsMaterialized = "PoolsMaterialized"
	conditionReady             = "Ready"
)

// ModelServiceReconciler compiles ModelService intent and owns ModelPool specs.
type ModelServiceReconciler struct {
	client.Client
}

// SetupWithManager registers the ModelService controller and its owned resources.
func (reconciler *ModelServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelService{}).
		Owns(&inferencev1alpha1.ModelPool{}).
		Watches(&inferencev1alpha1.KVService{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForKVService)).
		Complete(reconciler)
}

// Reconcile materializes stable ModelPools and aggregates their serving readiness.
func (reconciler *ModelServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := reconciler.Get(ctx, request.NamespacedName, service); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !service.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, service)
	}
	if !controllerutil.ContainsFinalizer(service, modelServiceFinalizer) {
		base := service.DeepCopy()
		controllerutil.AddFinalizer(service, modelServiceFinalizer)
		if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add ModelService finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	compiledPools, err := compiler.CompileModelService(service.Spec)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "InvalidIntent", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "CompilationFailed", "ModelService intent was not compiled"},
			ready:    conditionState{metav1.ConditionFalse, "InvalidIntent", "ModelService intent is invalid"},
		})
	}
	if err := reconciler.resolveManagedKVBindings(ctx, service, compiledPools); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "KVServiceNotReady", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "ResolutionFailed", "No new ModelPools were materialized"},
			ready:    conditionState{metav1.ConditionFalse, "KVServiceNotReady", "Referenced KVService is not ready"},
		})
	}

	if err := reconciler.reconcilePools(ctx, service, compiledPools); err != nil {
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
			pools:    conditionState{metav1.ConditionFalse, "ApplyFailed", "ModelPools were not fully materialized"},
			ready:    conditionState{metav1.ConditionFalse, "PoolsNotReady", "ModelPools are not ready"},
		})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	ready, readyReason, readyMessage, err := reconciler.serviceReadiness(ctx, service, compiledPools)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.updateStatus(ctx, service, modelServiceState{
		compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
		pools:    conditionState{metav1.ConditionTrue, "Applied", "All ModelPools were materialized"},
		ready:    conditionState{conditionStatus(ready), readyReason, readyMessage},
	}); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelServiceReconciler) reconcilePools(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) error {
	owned, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return err
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(owned))
	for index := range owned {
		pool := &owned[index]
		if previous := byPoolName[pool.Spec.PoolName]; previous != nil {
			return fmt.Errorf("ModelService owns duplicate ModelPools for poolName %q", pool.Spec.PoolName)
		}
		byPoolName[pool.Spec.PoolName] = pool
	}

	desired := make(map[string]struct{}, len(compiledPools))
	for _, compiled := range compiledPools {
		desired[compiled.Name] = struct{}{}
		pool := byPoolName[compiled.Name]
		if pool == nil {
			pool = &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Namespace: service.Namespace}}
			name := service.Name + "-" + compiled.Name
			if len(name) <= 63 {
				pool.Name = name
			} else {
				prefix := service.Name
				if len(prefix) > 52 {
					prefix = prefix[:52]
				}
				pool.GenerateName = prefix + "-"
			}
		}

		created := pool.ResourceVersion == ""
		before := pool.DeepCopy()
		pool.Spec = inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
			PoolName:        compiled.Name,
			DesiredGroups:   compiled.DesiredGroups,
			Template:        compiled.Template,
		}
		if err := controllerutil.SetControllerReference(service, pool, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set ModelPool %q owner: %w", compiled.Name, err)
		}

		if created {
			if err := reconciler.Create(ctx, pool); err != nil {
				return fmt.Errorf("create ModelPool %q: %w", compiled.Name, err)
			}
		} else if !reflect.DeepEqual(before.Spec, pool.Spec) || !reflect.DeepEqual(before.OwnerReferences, pool.OwnerReferences) {
			if err := reconciler.Update(ctx, pool); err != nil {
				return fmt.Errorf("update ModelPool %q: %w", compiled.Name, err)
			}
		}
	}

	for index := range owned {
		pool := &owned[index]
		if _, keep := desired[pool.Spec.PoolName]; keep {
			continue
		}
		if err := reconciler.Delete(ctx, pool); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("delete stale ModelPool %q: %w", pool.Name, err)
		}
	}
	return nil
}

func (reconciler *ModelServiceReconciler) serviceReadiness(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, string, string, error) {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, "PoolsNotReady", "ModelPools are not ready", err
	}
	if len(pools) != len(compiledPools) {
		return false, "PoolsNotReady", "ModelPool reconciliation has not converged", nil
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		byPoolName[pool.Spec.PoolName] = pool
	}
	hasCapacity := false
	for _, compiled := range compiledPools {
		if compiled.DesiredGroups == 0 {
			continue
		}
		hasCapacity = true
		pool := byPoolName[compiled.Name]
		if pool == nil || pool.Spec.DesiredGroups != compiled.DesiredGroups || !modelPoolReady(pool) {
			return false, "PoolsNotReady", "One or more ModelPools are not ready", nil
		}
	}
	if !hasCapacity {
		return false, "ScaledToZero", "ModelService has no requested serving capacity", nil
	}
	return true, "Ready", "All serving ModelPools are ready", nil
}

func modelPoolReady(pool *inferencev1alpha1.ModelPool) bool {
	if pool == nil || !pool.DeletionTimestamp.IsZero() || pool.Status.ObservedGeneration != pool.Generation {
		return false
	}
	condition := meta.FindStatusCondition(pool.Status.Conditions, conditionReady)
	return condition != nil &&
		condition.Status == metav1.ConditionTrue &&
		condition.ObservedGeneration == pool.Generation
}

func (reconciler *ModelServiceReconciler) reconcileDelete(ctx context.Context, service *inferencev1alpha1.ModelService) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(service, modelServiceFinalizer) {
		return ctrl.Result{}, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(pools) > 0 {
		for index := range pools {
			if err := reconciler.Delete(ctx, &pools[index]); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, fmt.Errorf("delete ModelPool %q: %w", pools[index].Name, err)
			}
		}
		return ctrl.Result{Requeue: true}, nil
	}

	base := service.DeepCopy()
	controllerutil.RemoveFinalizer(service, modelServiceFinalizer)
	if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return ctrl.Result{}, fmt.Errorf("remove ModelService finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelServiceReconciler) ownedPools(ctx context.Context, service *inferencev1alpha1.ModelService) ([]inferencev1alpha1.ModelPool, error) {
	var list inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &list, client.InNamespace(service.Namespace)); err != nil {
		return nil, fmt.Errorf("list ModelPools: %w", err)
	}
	owned := make([]inferencev1alpha1.ModelPool, 0, len(list.Items))
	for index := range list.Items {
		pool := &list.Items[index]
		referenceMatches := pool.Spec.ModelServiceRef.Name == service.Name && pool.Spec.ModelServiceRef.UID == string(service.UID)
		ownerMatches := metav1.IsControlledBy(pool, service)
		if referenceMatches != ownerMatches {
			return nil, fmt.Errorf("ModelPool %q has inconsistent ModelService ownership", pool.Name)
		}
		if referenceMatches {
			owned = append(owned, *pool)
		}
	}
	return owned, nil
}

type conditionState struct {
	Status  metav1.ConditionStatus
	Reason  string
	Message string
}

type modelServiceState struct {
	compiled conditionState
	pools    conditionState
	ready    conditionState
}

func (reconciler *ModelServiceReconciler) updateStatus(ctx context.Context, service *inferencev1alpha1.ModelService, state modelServiceState) error {
	base := service.DeepCopy()
	service.Status.ObservedGeneration = service.Generation
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionIntentCompiled, Status: state.compiled.Status, Reason: state.compiled.Reason, Message: state.compiled.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionPoolsMaterialized, Status: state.pools.Status, Reason: state.pools.Reason, Message: state.pools.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionReady, Status: state.ready.Status, Reason: state.ready.Reason, Message: state.ready.Message, ObservedGeneration: service.Generation})
	if reflect.DeepEqual(base.Status, service.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelService status: %w", err)
	}
	return nil
}

// resolveManagedKVBindings replaces a user name reference with the KVService UID and
// current binding contract before any ModelPool is created or changed.
func (reconciler *ModelServiceReconciler) resolveManagedKVBindings(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool) error {
	for i := range pools {
		store := modelServicePoolStore(service, pools[i].Name)
		if store == nil || store.KVServiceRef == nil {
			continue
		}
		kv := new(inferencev1alpha1.KVService)
		if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: store.KVServiceRef.Name}, kv); err != nil {
			return fmt.Errorf("get KVService %q: %w", store.KVServiceRef.Name, err)
		}
		ready := meta.FindStatusCondition(kv.Status.Conditions, conditionReady)
		if !kv.DeletionTimestamp.IsZero() || kv.Status.ObservedGeneration != kv.Generation || ready == nil || ready.Status != metav1.ConditionTrue || ready.ObservedGeneration != kv.Generation || kv.Status.Binding == nil || kv.Status.Binding.Revision == "" || kv.Status.Binding.ConfigMapName == "" || kv.Status.Binding.ConfigMapKey == "" || kv.Status.Binding.PythonHashSeed != "0" {
			return fmt.Errorf("KVService %q does not have a current Ready binding", kv.Name)
		}
		bufferBytes, err := requesterBufferBytes(kv)
		if err != nil {
			return fmt.Errorf("KVService %q requester buffer: %w", kv.Name, err)
		}
		if err := resourcevalidation.ValidateRequesterBufferBudget(pools[i].Template.Resources, bufferBytes); err != nil {
			return fmt.Errorf("modelPool %q requester buffer budget: %w", pools[i].Name, err)
		}
		pools[i].Template.KVCache.MooncakeStore = &inferencev1alpha1.NormalizedMooncakeStore{ManagedBinding: &inferencev1alpha1.ManagedMooncakeStoreBinding{Name: kv.Name, UID: string(kv.UID), BindingRevision: kv.Status.Binding.Revision, ConfigMapName: kv.Status.Binding.ConfigMapName, ConfigMapKey: kv.Status.Binding.ConfigMapKey, RequesterBufferBytes: bufferBytes}}
	}
	return nil
}

func modelServicePoolStore(service *inferencev1alpha1.ModelService, name string) *inferencev1alpha1.MooncakeStore {
	if name == "default" && len(service.Spec.ModelPools) == 0 && service.Spec.KVCache != nil {
		return service.Spec.KVCache.MooncakeStore
	}
	for i := range service.Spec.ModelPools {
		if service.Spec.ModelPools[i].Name == name && service.Spec.ModelPools[i].KVCache != nil {
			return service.Spec.ModelPools[i].KVCache.MooncakeStore
		}
	}
	return nil
}

func (reconciler *ModelServiceReconciler) modelServicesForKVService(ctx context.Context, object client.Object) []reconcile.Request {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for i := range services.Items {
		if modelServiceReferencesKV(&services.Items[i], object.GetName()) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&services.Items[i])})
		}
	}
	return requests
}

func requesterBufferBytes(service *inferencev1alpha1.KVService) (int64, error) {
	quantity, err := resource.ParseQuantity(string(service.Spec.Requester.LocalBufferSize))
	if err != nil {
		return 0, err
	}
	bytes, exact := quantity.AsInt64()
	if !exact || bytes < 1 {
		return 0, fmt.Errorf("must be a positive exact integer byte quantity")
	}
	return bytes, nil
}
