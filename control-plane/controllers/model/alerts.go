// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects model alert scope from the service-owned execution groups.

package model

import (
	"context"
	"errors"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/observability"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// reconcileAlerts restricts model rules to the current, ownership-verified execution groups.
func (reconciler *ServiceReconciler) reconcileAlerts(ctx context.Context, service *inferencev1alpha1.ModelService) error {
	var selected []string
	var thresholds *inferencev1alpha1.ModelAlertThresholds
	if service.Spec.Observability != nil && service.Spec.Observability.Alerts != nil {
		selected = service.Spec.Observability.Alerts.Rules
		thresholds = service.Spec.Observability.Alerts.Thresholds
	}
	scope := observability.Scope{Namespace: service.Namespace}
	var err error
	if len(selected) != 0 && service.DeletionTimestamp.IsZero() {
		scope, err = reconciler.alertScope(ctx, service)
	}
	if err == nil {
		err = reconciler.Alerts.Reconcile(ctx, service, selected, scope, thresholds, nil)
	}
	return errors.Join(err, observability.UpdateServiceCondition(ctx, reconciler.Client, service, len(selected) != 0, err))
}

// alertScope follows the service's verified ownership chain, excluding groups being removed.
func (reconciler *ServiceReconciler) alertScope(ctx context.Context, service *inferencev1alpha1.ModelService) (observability.Scope, error) {
	scope := observability.Scope{Namespace: service.Namespace}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return scope, err
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return scope, err
	}
	for index := range groups.Items {
		group := &groups.Items[index]
		for poolIndex := range pools {
			if servingconfig.RoutingGroupOwnedBy(group, &pools[poolIndex]) {
				scope.ModelGroups = append(scope.ModelGroups, group.Name)
				break
			}
		}
	}
	return scope, nil
}
