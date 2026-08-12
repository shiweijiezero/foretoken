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
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
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
		Complete(reconciler)
}

// Reconcile materializes stable ModelPools and summarizes their readiness.
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
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, metav1.ConditionFalse, "InvalidIntent", err.Error(), metav1.ConditionFalse, "CompilationFailed", "ModelService intent was not compiled", metav1.ConditionFalse)
	}

	if err := reconciler.reconcilePools(ctx, service, compiledPools); err != nil {
		statusErr := reconciler.updateStatus(ctx, service, metav1.ConditionTrue, "Compiled", "ModelService intent was compiled", metav1.ConditionFalse, "ApplyFailed", "ModelPools were not fully materialized", metav1.ConditionFalse)
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.updateStatus(ctx, service, metav1.ConditionTrue, "Compiled", "ModelService intent was compiled", metav1.ConditionTrue, "Applied", "All ModelPools were materialized", boolCondition(modelPoolsReady(pools, len(compiledPools)))); err != nil {
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

func (reconciler *ModelServiceReconciler) updateStatus(ctx context.Context, service *inferencev1alpha1.ModelService, compiledStatus metav1.ConditionStatus, compiledReason, compiledMessage string, poolsStatus metav1.ConditionStatus, poolsReason, poolsMessage string, readyStatus metav1.ConditionStatus) error {
	base := service.DeepCopy()
	service.Status.ObservedGeneration = service.Generation
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionIntentCompiled, Status: compiledStatus, Reason: compiledReason, Message: compiledMessage, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionPoolsMaterialized, Status: poolsStatus, Reason: poolsReason, Message: poolsMessage, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionReady, Status: readyStatus, Reason: modelServiceReadyReason(readyStatus), Message: modelServiceReadyMessage(readyStatus), ObservedGeneration: service.Generation})
	if reflect.DeepEqual(base.Status, service.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelService status: %w", err)
	}
	return nil
}

func modelPoolsReady(pools []inferencev1alpha1.ModelPool, expected int) bool {
	if len(pools) != expected {
		return false
	}
	for index := range pools {
		condition := meta.FindStatusCondition(pools[index].Status.Conditions, conditionPoolReady)
		if condition == nil || condition.Status != metav1.ConditionTrue {
			return false
		}
	}
	return true
}

func modelServiceReadyReason(status metav1.ConditionStatus) string {
	if status == metav1.ConditionTrue {
		return "PoolsReady"
	}
	return "PoolsPending"
}

func modelServiceReadyMessage(status metav1.ConditionStatus) string {
	if status == metav1.ConditionTrue {
		return "Every ModelPool is ready"
	}
	return "Waiting for every ModelPool to become ready"
}
