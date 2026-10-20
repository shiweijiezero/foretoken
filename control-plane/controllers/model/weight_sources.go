// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles engine-projected weight discovery Services within each model cohort.
package model

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// reconcileWeightSources retains discovery for every live cohort and removes only retired Pool-owned Services.
func (reconciler *PoolReconciler) reconcileWeightSources(ctx context.Context, pool *inferencev1alpha1.ModelPool, target resolver.ModelGroupTemplate) error {
	groups, err := ownedModelGroups(ctx, reconciler.APIReader, pool)
	if err != nil {
		return err
	}
	cohorts := make(map[string]inferencev1alpha1.ModelGroupSpec)
	for _, group := range groups {
		cohorts[group.Spec.Revision] = group.Spec
	}
	if pool.Spec.DesiredGroups > 0 {
		cohorts[target.Revision] = target.Spec(pool, 0)
	}
	desired := make(map[string]struct{})
	for _, spec := range cohorts {
		for _, projection := range vllmconfig.WeightSourceServices(spec) {
			name := projection.Name
			desired[name] = struct{}{}
			service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: pool.Namespace}}
			_, err := controllerutil.CreateOrUpdate(ctx, reconciler.Client, service, func() error {
				if service.ResourceVersion != "" && !metav1.IsControlledBy(service, pool) {
					return fmt.Errorf("weight source Service %q belongs to another owner", name)
				}
				if err := controllerutil.SetControllerReference(pool, service, reconciler.Scheme()); err != nil {
					return err
				}
				service.Labels = projection.Labels
				service.Spec.Selector = projection.Spec.Selector
				service.Spec.PublishNotReadyAddresses = projection.Spec.PublishNotReadyAddresses
				service.Spec.Ports = projection.Spec.Ports
				return nil
			})
			if err != nil {
				return fmt.Errorf("reconcile weight source Service: %w", err)
			}
		}
	}
	var services corev1.ServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(pool.Namespace), client.MatchingLabels{vllmconfig.WeightSourcePoolLabel: string(pool.UID)}); err != nil {
		return err
	}
	for index := range services.Items {
		service := &services.Items[index]
		if _, keep := desired[service.Name]; keep || !metav1.IsControlledBy(service, pool) {
			continue
		}
		if err := client.IgnoreNotFound(reconciler.Delete(ctx, service)); err != nil {
			return err
		}
	}
	return nil
}
