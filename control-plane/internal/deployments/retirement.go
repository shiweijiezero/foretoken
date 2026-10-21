// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retires instance resources before removing their controller and delegated permissions.
package deployments

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// retire preserves the finalizer executor through serving, source collection, and storage retirement.
func (r *Reconciler) retire(ctx context.Context, deployment *api.InferenceDeployment) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(deployment, deploymentFinalizer) {
		return ctrl.Result{}, nil
	}
	waiting := ctrl.Result{RequeueAfter: 2 * time.Second}
	pending := false
	for _, objects := range []client.ObjectList{&api.ProfileRunList{}, &api.VideoTaskList{}} {
		remaining, err := r.retireMembers(ctx, deployment, objects, true)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || remaining
	}
	if pending {
		return waiting, nil
	}
	for _, objects := range []client.ObjectList{&api.FrontendServiceList{}, &api.ModelServiceList{}, &api.KVServiceList{}} {
		remaining, err := r.retireMembers(ctx, deployment, objects, true)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || remaining
	}
	// Service deletion can precede owner-reference garbage collection. Pools keep
	// the controller alive while draining Groups and releasing model-cache records.
	for _, objects := range []client.ObjectList{&api.ModelPoolList{}, &api.ModelGroupList{}, &api.KVPoolList{}, &api.KVGroupList{}} {
		remaining, err := r.retireMembers(ctx, deployment, objects, false)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || remaining
	}
	if pending {
		return waiting, nil
	}

	remaining, err := r.retireMembers(ctx, deployment, &api.RuntimeCacheList{}, true)
	if err != nil {
		return ctrl.Result{}, err
	}
	pending = remaining
	references := slices.Clone(deployment.Spec.Resources)
	configuration := new(corev1.ConfigMap)
	key := client.ObjectKey{Namespace: deployment.Namespace, Name: ResourceName(string(deployment.UID))}
	if err := r.APIReader.Get(ctx, key, configuration); err == nil {
		if configuration.Labels[api.DeploymentUIDLabel] != string(deployment.UID) {
			return ctrl.Result{}, fmt.Errorf("controller configuration %s has another owner", key)
		}
		var selection controllerSelection
		if err := json.Unmarshal([]byte(configuration.Data[selectionKey]), &selection); err != nil {
			return ctrl.Result{}, err
		}
		for _, reference := range selection.Resources {
			if !slices.Contains(references, reference) {
				references = append(references, reference)
			}
		}
	} else if !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	for _, reference := range references {
		object := &unstructured.Unstructured{}
		object.SetAPIVersion(reference.APIVersion)
		object.SetKind(reference.Kind)
		namespaced, err := r.IsObjectNamespaced(object)
		if meta.IsNoMatchError(err) {
			continue
		}
		if err != nil {
			return ctrl.Result{}, err
		}
		if !namespaced {
			return ctrl.Result{}, fmt.Errorf("deployment resource %s/%s must be namespaced", reference.Kind, reference.Name)
		}
		if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: deployment.Namespace, Name: reference.Name}, object); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return ctrl.Result{}, err
		}
		if object.GetLabels()[api.DeploymentUIDLabel] != string(deployment.UID) {
			continue
		}
		if err := r.deleteObserved(ctx, object); err != nil {
			return ctrl.Result{}, err
		}
		pending = true
	}
	if pending {
		return waiting, nil
	}

	// Foreground retirement of the controller completes before its credentials,
	// election lease and delegated permissions are removed.
	controller := new(appsv1.Deployment)
	if err := r.APIReader.Get(ctx, key, controller); err == nil {
		if controller.Labels[api.DeploymentUIDLabel] != string(deployment.UID) {
			return ctrl.Result{}, fmt.Errorf("controller %s has another owner", key)
		}
		if err := r.Delete(ctx, controller, client.PropagationPolicy("Foreground"), client.Preconditions{UID: &controller.UID, ResourceVersion: &controller.ResourceVersion}); client.IgnoreNotFound(err) != nil {
			return ctrl.Result{}, err
		}
		return waiting, nil
	} else if !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	// Prometheus permissions can live beside a shared Prometheus. Select them by
	// the instance UID rather than assuming all delegated resources use its namespace.
	for _, objects := range []client.ObjectList{&rbacv1.RoleBindingList{}, &rbacv1.RoleList{}, &rbacv1.ClusterRoleBindingList{}, &rbacv1.ClusterRoleList{}, &corev1.ServiceAccountList{}, &corev1.ServiceList{}, &corev1.SecretList{}} {
		if err := r.APIReader.List(ctx, objects, client.MatchingLabels{api.DeploymentUIDLabel: string(deployment.UID), "app.kubernetes.io/component": controllerComponent}); err != nil {
			return ctrl.Result{}, err
		}
		items, err := meta.ExtractList(objects)
		if err != nil {
			return ctrl.Result{}, err
		}
		for _, item := range items {
			if err := r.deleteObserved(ctx, item.(client.Object)); err != nil {
				return ctrl.Result{}, err
			}
			pending = true
		}
	}
	if pending {
		return waiting, nil
	}
	lease := &coordinationv1.Lease{}
	if err := r.APIReader.Get(ctx, key, lease); err == nil {
		if err := r.deleteObserved(ctx, lease); err != nil {
			return ctrl.Result{}, err
		}
	} else if !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	if configuration.UID != "" {
		if err := r.deleteObserved(ctx, configuration); err != nil {
			return ctrl.Result{}, err
		}
	}
	r.leaders.Delete(deployment.UID)
	base := deployment.DeepCopy()
	controllerutil.RemoveFinalizer(deployment, deploymentFinalizer)
	return ctrl.Result{}, r.Patch(ctx, deployment, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
}

// retireMembers lists only one instance; native owner GC retires its derived execution resources.
func (r *Reconciler) retireMembers(ctx context.Context, deployment *api.InferenceDeployment, objects client.ObjectList, remove bool) (bool, error) {
	if err := r.APIReader.List(ctx, objects, client.InNamespace(deployment.Namespace), client.MatchingLabels{api.DeploymentUIDLabel: string(deployment.UID)}); err != nil {
		return false, err
	}
	items, err := meta.ExtractList(objects)
	if err != nil {
		return false, err
	}
	if remove {
		for _, item := range items {
			if err := r.deleteObserved(ctx, item.(client.Object)); err != nil {
				return false, err
			}
		}
	}
	return len(items) != 0, nil
}

func (r *Reconciler) deleteObserved(ctx context.Context, object client.Object) error {
	if !object.GetDeletionTimestamp().IsZero() {
		return nil
	}
	uid, version := object.GetUID(), object.GetResourceVersion()
	return client.IgnoreNotFound(r.Delete(ctx, object, client.Preconditions{UID: &uid, ResourceVersion: &version}))
}
