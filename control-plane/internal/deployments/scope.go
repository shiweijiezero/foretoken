// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package deployments confines instance controllers to their own resources and publications.
package deployments

import (
	"context"
	"encoding/json"
	"fmt"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

// Scope identifies the Kubernetes namespace and immutable deployment UID of one controller.
type Scope struct {
	Namespace string
	UID       string
}

// ResourceName names platform-owned instance resources without exposing source paths or content hashes.
func ResourceName(uid string) string { return "foretoken-" + uid }

// Selector is shared by watches, direct lists, and collection deletion.
func (scope Scope) Selector() labels.Selector {
	return labels.SelectorFromSet(map[string]string{api.DeploymentUIDLabel: scope.UID})
}

// Manager gives controllers the same ownership boundary through cached and direct clients.
// Platform bootstrap and model-cache collection continue to use their unscoped managers.
func (scope Scope) Manager(base manager.Manager) manager.Manager {
	reader := &scopedReader{Reader: base.GetAPIReader(), metadata: base.GetClient(), scope: scope}
	writer := &scopedClient{Client: base.GetClient(), reader: reader, scope: scope}
	return &scopedManager{Manager: base, client: writer, reader: reader}
}

type scopedManager struct {
	manager.Manager
	client client.Client
	reader client.Reader
}

func (manager *scopedManager) GetClient() client.Client    { return manager.client }
func (manager *scopedManager) GetAPIReader() client.Reader { return manager.reader }

type scopedReader struct {
	client.Reader
	metadata client.Client
	scope    Scope
}

// Shared credentials and explicitly selected storage are read by name, not discovered as instance members.
func sharedRead(object client.Object) bool {
	switch object.(type) {
	case *corev1.Secret, *corev1.PersistentVolumeClaim, *corev1.ConfigMap, *monitoringv1.Prometheus:
		return true
	case *unstructured.Unstructured:
		kind := object.GetObjectKind().GroupVersionKind().GroupKind()
		return kind == corev1.SchemeGroupVersion.WithKind("Secret").GroupKind() ||
			kind == corev1.SchemeGroupVersion.WithKind("PersistentVolumeClaim").GroupKind() ||
			kind == corev1.SchemeGroupVersion.WithKind("ConfigMap").GroupKind() ||
			kind == monitoringv1.SchemeGroupVersion.WithKind("Prometheus").GroupKind()
	default:
		return false
	}
}

func (reader *scopedReader) Get(ctx context.Context, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
	if err := reader.Reader.Get(ctx, key, object, options...); err != nil {
		return err
	}
	namespaced, err := reader.metadata.IsObjectNamespaced(object)
	if err != nil || !namespaced || sharedRead(object) {
		return err
	}
	if object.GetNamespace() != reader.scope.Namespace || object.GetLabels()[api.DeploymentUIDLabel] != reader.scope.UID {
		kind, err := reader.metadata.GroupVersionKindFor(object)
		if err != nil {
			return err
		}
		return apierrors.NewNotFound(schema.GroupResource{Group: kind.Group, Resource: kind.Kind}, key.Name)
	}
	return nil
}

func (reader *scopedReader) List(ctx context.Context, objects client.ObjectList, options ...client.ListOption) error {
	namespaced, err := reader.metadata.IsObjectNamespaced(objects)
	if err != nil {
		return err
	}
	if namespaced {
		options = append(options, client.InNamespace(reader.scope.Namespace), client.MatchingLabels{api.DeploymentUIDLabel: reader.scope.UID})
	}
	return reader.Reader.List(ctx, objects, options...)
}

type scopedClient struct {
	client.Client
	reader *scopedReader
	scope  Scope
}

func (c *scopedClient) Get(ctx context.Context, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
	// ConfigMaps and claims can be supplied by the administrator without an instance label.
	if sharedRead(object) {
		return c.reader.Get(ctx, key, object, options...)
	}
	return (&scopedReader{Reader: c.Client, metadata: c.Client, scope: c.scope}).Get(ctx, key, object, options...)
}

func (c *scopedClient) List(ctx context.Context, objects client.ObjectList, options ...client.ListOption) error {
	return (&scopedReader{Reader: c.Client, metadata: c.Client, scope: c.scope}).List(ctx, objects, options...)
}

// Namespace KV credentials remain shared and outlive individual deployments.
func sharedCredential(object client.Object) bool {
	_, secret := object.(*corev1.Secret)
	if value, ok := object.(*unstructured.Unstructured); ok {
		secret = value.GroupVersionKind().GroupKind() == corev1.SchemeGroupVersion.WithKind("Secret").GroupKind()
	}
	return secret && object.GetName() == servingconfig.KVIndexerSecretName
}

func (scope Scope) label(metadata metav1.Object) error {
	if selected := metadata.GetLabels()[api.DeploymentUIDLabel]; selected != "" && selected != scope.UID {
		return fmt.Errorf("resource %q belongs to another deployment", metadata.GetName())
	}
	values := metadata.GetLabels()
	if values == nil {
		values = make(map[string]string)
	}
	values[api.DeploymentUIDLabel] = scope.UID
	metadata.SetLabels(values)
	return nil
}

// prepare propagates instance membership through the workload controllers that create Pods.
func (c *scopedClient) prepare(object client.Object) error {
	namespaced, err := c.IsObjectNamespaced(object)
	if err != nil {
		return err
	}
	if !namespaced || object.GetNamespace() != c.scope.Namespace {
		return fmt.Errorf("instance controllers may write only resources in their own namespace")
	}
	if sharedCredential(object) {
		return nil
	}
	if err := c.scope.label(object); err != nil {
		return err
	}
	switch object := object.(type) {
	case *appsv1.Deployment:
		return c.scope.label(&object.Spec.Template)
	case *appsv1.StatefulSet:
		return c.scope.label(&object.Spec.Template)
	case *batchv1.Job:
		return c.scope.label(&object.Spec.Template)
	case *lwsv1.LeaderWorkerSet:
		if object.Spec.LeaderWorkerTemplate.LeaderTemplate != nil {
			if err := c.scope.label(object.Spec.LeaderWorkerTemplate.LeaderTemplate); err != nil {
				return err
			}
		}
		return c.scope.label(&object.Spec.LeaderWorkerTemplate.WorkerTemplate)
	case *unstructured.Unstructured:
		var templates [][]string
		switch object.GroupVersionKind().GroupKind() {
		case appsv1.SchemeGroupVersion.WithKind("Deployment").GroupKind(), appsv1.SchemeGroupVersion.WithKind("StatefulSet").GroupKind(), batchv1.SchemeGroupVersion.WithKind("Job").GroupKind():
			templates = [][]string{{"spec", "template"}}
		case lwsv1.GroupVersion.WithKind("LeaderWorkerSet").GroupKind():
			templates = [][]string{{"spec", "leaderWorkerTemplate", "workerTemplate"}}
			if _, exists, err := unstructured.NestedMap(object.Object, "spec", "leaderWorkerTemplate", "leaderTemplate"); err != nil {
				return err
			} else if exists {
				templates = append(templates, []string{"spec", "leaderWorkerTemplate", "leaderTemplate"})
			}
		}
		for _, template := range templates {
			fields := append(template, "metadata", "labels")
			values, _, err := unstructured.NestedStringMap(object.Object, fields...)
			if err != nil {
				return err
			}
			metadata := metav1.ObjectMeta{Labels: values}
			if err := c.scope.label(&metadata); err != nil {
				return err
			}
			if err := unstructured.SetNestedStringMap(object.Object, metadata.Labels, fields...); err != nil {
				return err
			}
		}
	}
	return nil
}

// checkWrite reads fresh API metadata so desired labels and filtered-cache absence cannot authorize takeover.
func (c *scopedClient) checkWrite(ctx context.Context, object client.Object) error {
	gvk, err := c.GroupVersionKindFor(object)
	if err != nil {
		return err
	}
	current := &metav1.PartialObjectMetadata{}
	current.SetGroupVersionKind(gvk)
	if err := c.reader.Reader.Get(ctx, client.ObjectKeyFromObject(object), current); err != nil {
		return err
	}
	if current.GetNamespace() != c.scope.Namespace || !sharedCredential(object) && current.GetLabels()[api.DeploymentUIDLabel] != c.scope.UID {
		return fmt.Errorf("resource %s/%s is not owned by this deployment", current.GetNamespace(), current.GetName())
	}
	if object.GetUID() != "" && object.GetUID() != current.GetUID() {
		return fmt.Errorf("resource %s/%s was replaced", current.GetNamespace(), current.GetName())
	}
	if object.GetUID() == "" {
		object.SetUID(current.GetUID())
	}
	if object.GetResourceVersion() == "" {
		object.SetResourceVersion(current.GetResourceVersion())
	}
	return nil
}

func (c *scopedClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if err := c.prepare(object); err != nil {
		return err
	}
	return c.Client.Create(ctx, object, options...)
}

func (c *scopedClient) Update(ctx context.Context, object client.Object, options ...client.UpdateOption) error {
	if err := c.prepare(object); err != nil {
		return err
	}
	if err := c.checkWrite(ctx, object); err != nil {
		return err
	}
	return c.Client.Update(ctx, object, options...)
}

// identityPatch puts the observed incarnation on the wire, including patches whose
// diff omits unchanged metadata. Existing nonempty caller preconditions remain intact.
func identityPatch(object client.Object, patch client.Patch) (client.Patch, error) {
	data, err := patch.Data(object)
	if err != nil {
		return nil, err
	}
	identity := map[string]string{"uid": string(object.GetUID()), "resourceVersion": object.GetResourceVersion()}
	if patch.Type() == types.JSONPatchType {
		var operations []json.RawMessage
		if err := json.Unmarshal(data, &operations); err != nil {
			return nil, err
		}
		tests := make([]json.RawMessage, 0, 2+len(operations))
		for _, key := range []string{"uid", "resourceVersion"} {
			encoded, err := json.Marshal(map[string]string{"op": "test", "path": "/metadata/" + key, "value": identity[key]})
			if err != nil {
				return nil, err
			}
			tests = append(tests, encoded)
		}
		data, err = json.Marshal(append(tests, operations...))
	} else {
		var document map[string]json.RawMessage
		if err := json.Unmarshal(data, &document); err != nil {
			return nil, err
		}
		if document == nil {
			return nil, fmt.Errorf("patch must be an object")
		}
		metadata := make(map[string]json.RawMessage)
		if encoded, ok := document["metadata"]; ok {
			if err := json.Unmarshal(encoded, &metadata); err != nil {
				return nil, err
			}
			if metadata == nil {
				return nil, fmt.Errorf("patch metadata must be an object")
			}
		}
		for key, value := range identity {
			var selected string
			if encoded, present := metadata[key]; present {
				if err := json.Unmarshal(encoded, &selected); err != nil {
					return nil, err
				}
			}
			if selected == "" {
				metadata[key], err = json.Marshal(value)
				if err != nil {
					return nil, err
				}
			}
		}
		document["metadata"], err = json.Marshal(metadata)
		if err != nil {
			return nil, err
		}
		data, err = json.Marshal(document)
	}
	if err != nil {
		return nil, err
	}
	return client.RawPatch(patch.Type(), data), nil
}

// Patch fences updates to the observed incarnation and preserves apply options when creating a missing target.
func (c *scopedClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	if err := c.prepare(object); err != nil {
		return err
	}
	if err := c.checkWrite(ctx, object); err != nil {
		if !apierrors.IsNotFound(err) || patch.Type() != types.ApplyPatchType || object.GetUID() != "" {
			return err
		}
		settings := new(client.PatchOptions).ApplyOptions(options).AsPatchOptions()
		return c.Client.Create(ctx, object, &client.CreateOptions{
			FieldManager: settings.FieldManager, FieldValidation: settings.FieldValidation, DryRun: settings.DryRun,
		})
	}
	patch, err := identityPatch(object, patch)
	if err != nil {
		return err
	}
	return c.Client.Patch(ctx, object, patch, options...)
}

// Delete preserves the caller's cascade policy while fencing deletion to the observed resource incarnation.
func (c *scopedClient) Delete(ctx context.Context, object client.Object, options ...client.DeleteOption) error {
	if sharedCredential(object) {
		return fmt.Errorf("namespace KV credentials do not belong to an individual deployment")
	}
	if err := c.checkWrite(ctx, object); err != nil {
		return err
	}
	settings := new(client.DeleteOptions).ApplyOptions(options)
	if settings.Preconditions == nil {
		settings.Preconditions = &metav1.Preconditions{}
	}
	if settings.Preconditions.UID == nil {
		uid := object.GetUID()
		settings.Preconditions.UID = &uid
	}
	if settings.Preconditions.ResourceVersion == nil {
		version := object.GetResourceVersion()
		settings.Preconditions.ResourceVersion = &version
	}
	return c.Client.Delete(ctx, object, settings)
}

func (c *scopedClient) DeleteAllOf(ctx context.Context, object client.Object, options ...client.DeleteAllOfOption) error {
	options = append(options, client.InNamespace(c.scope.Namespace), client.MatchingLabels{api.DeploymentUIDLabel: c.scope.UID})
	return c.Client.DeleteAllOf(ctx, object, options...)
}

func applyObject(configuration runtime.ApplyConfiguration) (*unstructured.Unstructured, error) {
	data, err := json.Marshal(configuration)
	if err != nil {
		return nil, err
	}
	object := new(unstructured.Unstructured)
	return object, object.UnmarshalJSON(data)
}

func (c *scopedClient) Apply(ctx context.Context, configuration runtime.ApplyConfiguration, options ...client.ApplyOption) error {
	object, err := applyObject(configuration)
	if err != nil {
		return err
	}
	settings := new(client.ApplyOptions).ApplyOptions(options)
	return c.Patch(ctx, object, client.Apply, &client.PatchOptions{Raw: settings.AsPatchOptions()})
}

func (c *scopedClient) Status() client.SubResourceWriter { return c.SubResource("status") }

func (c *scopedClient) SubResource(name string) client.SubResourceClient {
	return &scopedSubresource{SubResourceClient: c.Client.SubResource(name), owner: c}
}

type scopedSubresource struct {
	client.SubResourceClient
	owner *scopedClient
}

func (sub *scopedSubresource) Get(ctx context.Context, object, result client.Object, options ...client.SubResourceGetOption) error {
	if err := sub.owner.checkWrite(ctx, object); err != nil {
		return err
	}
	return sub.SubResourceClient.Get(ctx, object, result, options...)
}

func (sub *scopedSubresource) Create(ctx context.Context, object, body client.Object, options ...client.SubResourceCreateOption) error {
	if err := sub.owner.checkWrite(ctx, object); err != nil {
		return err
	}
	return sub.SubResourceClient.Create(ctx, object, body, options...)
}

func (sub *scopedSubresource) Update(ctx context.Context, object client.Object, options ...client.SubResourceUpdateOption) error {
	if err := sub.owner.checkWrite(ctx, object); err != nil {
		return err
	}
	return sub.SubResourceClient.Update(ctx, object, options...)
}

func (sub *scopedSubresource) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.SubResourcePatchOption) error {
	if err := sub.owner.checkWrite(ctx, object); err != nil {
		return err
	}
	patch, err := identityPatch(object, patch)
	if err != nil {
		return err
	}
	return sub.SubResourceClient.Patch(ctx, object, patch, options...)
}

func (sub *scopedSubresource) Apply(ctx context.Context, configuration runtime.ApplyConfiguration, options ...client.SubResourceApplyOption) error {
	object, err := applyObject(configuration)
	if err != nil {
		return err
	}
	settings := new(client.SubResourceApplyOptions)
	for _, option := range options {
		option.ApplyToSubResourceApply(settings)
	}
	return sub.Patch(ctx, object, client.Apply, &client.SubResourcePatchOptions{PatchOptions: client.PatchOptions{Raw: settings.AsPatchOptions()}})
}
