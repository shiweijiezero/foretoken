// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes Pool-owned live instance limits and observes their ingress acknowledgements.

package controllers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	instanceAdmissionKey               = "instance-admission.json"
	instanceAdmissionVolumeName        = "instance-admission"
	instanceAdmissionConfigEnv         = "FORETOKEN_INSTANCE_ADMISSION_CONFIG"
	instanceAdmissionDirectory         = "/etc/foretoken/instance-admission"
	instanceAdmissionReadyPath         = "/v1/internal/admission/ready"
	instanceAdmissionRefreshAnnotation = "inference.foretoken.io/instance-admission-resource-version"
)

type instanceAdmissionConfiguration struct {
	Version               uint64  `json:"version"`
	MaxConcurrentRequests *uint32 `json:"max_concurrent_requests"`
}

func instanceAdmissionConfigMapName(poolUID string) string {
	return "instance-admission-" + poolUID
}

func groupUsesLiveInstanceAdmission(group *inferencev1alpha1.ModelGroup) bool {
	return group.Spec.Runtime.Backend == "vllm" && group.Spec.Runtime.InstanceAdmissionProtocol == inferencev1alpha1.InstanceAdmissionFileProtocol
}

func instanceAdmissionLimit(config *inferencev1alpha1.InstanceAdmissionConfig) *uint32 {
	if config == nil {
		return nil
	}
	limit := config.MaxConcurrentRequests
	return &limit
}

// readInstanceAdmissionConfiguration validates the publisher's complete persisted contract.
func readInstanceAdmissionConfiguration(data string) (instanceAdmissionConfiguration, error) {
	var config instanceAdmissionConfiguration
	decoder := json.NewDecoder(bytes.NewBufferString(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return config, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(data), &fields); err != nil {
		return config, err
	}
	if config.Version == 0 || fields["max_concurrent_requests"] == nil || config.MaxConcurrentRequests != nil && *config.MaxConcurrentRequests == 0 {
		return config, fmt.Errorf("instance admission requires a positive version and a positive or null max_concurrent_requests")
	}
	return config, nil
}

// reconcileInstanceAdmission is the sole publisher of live instance limits.
// Pool spec generation identifies changed or repaired configuration; unrelated spec
// changes retain the publication. Reserving its version in Pool status before writing
// the ConfigMap prevents restoration of an older payload from lowering the runtime target.
// Workload membership changes refresh new ingress Pods without changing immutable Group contracts.
func (reconciler *ModelPoolReconciler) reconcileInstanceAdmission(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate) error {
	groups, err := ownedModelGroups(ctx, reconciler.Client, pool)
	if err != nil {
		return err
	}
	needed := template.Runtime.Backend == "vllm" && template.Runtime.InstanceAdmissionProtocol == inferencev1alpha1.InstanceAdmissionFileProtocol
	for index := range groups {
		needed = needed || groupUsesLiveInstanceAdmission(&groups[index])
	}
	if !needed {
		return nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	configuration := new(corev1.ConfigMap)
	key := client.ObjectKey{Namespace: pool.Namespace, Name: instanceAdmissionConfigMapName(string(pool.UID))}
	err = reader.Get(ctx, key, configuration)
	created := apierrors.IsNotFound(err)
	if err != nil && !created {
		return fmt.Errorf("get instance admission ConfigMap: %w", err)
	}
	config := instanceAdmissionConfiguration{Version: max(uint64(pool.Generation), pool.Status.InstanceAdmissionVersion), MaxConcurrentRequests: instanceAdmissionLimit(pool.Spec.InstanceAdmission)}
	if !created {
		if !metav1.IsControlledBy(configuration, pool) {
			return fmt.Errorf("ConfigMap %q is not controlled by ModelPool", configuration.Name)
		}
		previous, err := readInstanceAdmissionConfiguration(configuration.Data[instanceAdmissionKey])
		// Pool intent remains authoritative when the owned projection is damaged.
		configurationChanged := err != nil || previous.Version < pool.Status.InstanceAdmissionVersion || previous.Version > uint64(pool.Generation) || !reflect.DeepEqual(previous.MaxConcurrentRequests, config.MaxConcurrentRequests)
		if !configurationChanged {
			config.Version = previous.Version
		}
	}
	if pool.Status.InstanceAdmissionVersion < config.Version {
		base := pool.DeepCopy()
		pool.Status.InstanceAdmissionVersion = config.Version
		if err := reconciler.Status().Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("reserve instance admission publication version: %w", err)
		}
	}
	payload, err := json.Marshal(config)
	if err != nil {
		return fmt.Errorf("encode instance admission: %w", err)
	}
	if created {
		configuration.ObjectMeta = metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}
		configuration.Data = map[string]string{instanceAdmissionKey: string(payload)}
		if err := controllerutil.SetControllerReference(pool, configuration, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set instance admission ConfigMap owner: %w", err)
		}
		if err := reconciler.Create(ctx, configuration); err != nil {
			return fmt.Errorf("create instance admission ConfigMap: %w", err)
		}
	} else if configuration.Data[instanceAdmissionKey] != string(payload) {
		if configuration.Data == nil {
			configuration.Data = make(map[string]string)
		}
		configuration.Data[instanceAdmissionKey] = string(payload)
		// Update retains resourceVersion so concurrent publication cannot overwrite a newer limit.
		if err := reconciler.Update(ctx, configuration); err != nil {
			return fmt.Errorf("update instance admission ConfigMap: %w", err)
		}
	}
	var pods corev1.PodList
	if err := reader.List(ctx, &pods, client.InNamespace(pool.Namespace)); err != nil {
		return fmt.Errorf("list instance admission Pods: %w", err)
	}
	// ConfigMap resourceVersion also changes on same-version repairs; it is only a
	// projection refresh token, while runtime acknowledgement uses the payload version.
	refreshToken := configuration.ResourceVersion
	for index := range pods.Items {
		pod := &pods.Items[index]
		if !pod.DeletionTimestamp.IsZero() || pod.Labels[modelGroupLabel] == "" || pod.Annotations[instanceAdmissionRefreshAnnotation] == refreshToken {
			continue
		}
		group, err := backendPodGroup(ctx, reader, pod)
		if apierrors.IsNotFound(err) || errors.Is(err, errUnprovenBackendOwner) {
			continue
		}
		if err != nil {
			return err
		}
		if !modelGroupOwnedBy(group, pool) || !groupUsesLiveInstanceAdmission(group) || !labels.SelectorFromSet(modelGroupServiceSelector(group)).Matches(labels.Set(pod.Labels)) {
			continue
		}
		base := pod.DeepCopy()
		if pod.Annotations == nil {
			pod.Annotations = make(map[string]string)
		}
		pod.Annotations[instanceAdmissionRefreshAnnotation] = refreshToken
		if err := reconciler.Patch(ctx, pod, client.MergeFrom(base)); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("refresh instance Pod %q configuration: %w", pod.Name, err)
		}
	}
	return nil
}

// poolsForInstancePod refreshes newly created ingress Pods through their existing Group owner.
func (reconciler *ModelPoolReconciler) poolsForInstancePod(ctx context.Context, object client.Object) []reconcile.Request {
	if object.GetLabels()[modelGroupLabel] == "" {
		return nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	pod := object.(*corev1.Pod)
	group, err := backendPodGroup(ctx, reader, pod)
	if err != nil || !groupUsesLiveInstanceAdmission(group) || !labels.SelectorFromSet(modelGroupServiceSelector(group)).Matches(labels.Set(pod.Labels)) {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}}}
}

// modelServicesForInstanceConfiguration observes publications through their verified Pool owner.
func (reconciler *ModelServiceReconciler) modelServicesForInstanceConfiguration(ctx context.Context, object client.Object) []reconcile.Request {
	owner := metav1.GetControllerOf(object)
	if owner == nil || owner.APIVersion != inferencev1alpha1.GroupVersion.String() || owner.Kind != "ModelPool" {
		return nil
	}
	pool := new(inferencev1alpha1.ModelPool)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: object.GetNamespace(), Name: owner.Name}, pool); err != nil || !metav1.IsControlledBy(object, pool) || object.GetName() != instanceAdmissionConfigMapName(string(pool.UID)) {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}}}
}

type instanceAdmissionDiagnostics struct {
	ActiveGeneration      *uint64 `json:"active_generation"`
	TargetGeneration      *uint64 `json:"target_generation"`
	ConfigurationError    *string `json:"configuration_error"`
	MaxConcurrentRequests *uint32 `json:"max_concurrent_requests"`
	Accepting             bool    `json:"accepting"`
}

// instanceAdmissionApplied observes every requested ingress of the current serving generation.
// Desired live limits must match the materialized Pool and its owned publication before probing;
// commitServingGeneration owns structural template consistency.
func (reconciler *ModelServiceReconciler) instanceAdmissionApplied(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, string, string, error) {
	pending := func() (bool, string, string, error) {
		return false, "ConfigurationPending", "Waiting for model instances to apply instance admission configuration", nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := reader.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		return false, "", "", err
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reader.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, "", "", err
	}
	for _, desired := range compiledPools {
		if desired.DesiredGroups == 0 || desired.Template.Backend != "vllm" {
			continue
		}
		var pool *inferencev1alpha1.ModelPool
		for index := range pools.Items {
			candidate := &pools.Items[index]
			if routingPoolOwnedBy(candidate, service) && candidate.Spec.PoolName == desired.Name {
				pool = candidate
				break
			}
		}
		if pool == nil || !reflect.DeepEqual(pool.Spec.InstanceAdmission, desired.InstanceAdmission) {
			return pending()
		}
		selected := serviceServingRevision(service, pool)
		needsConfig := pool.Spec.Template.Application != nil && pool.Spec.Template.Application.InstanceAdmissionProtocol == inferencev1alpha1.InstanceAdmissionFileProtocol
		for index := range groups.Items {
			group := &groups.Items[index]
			if routingGroupOwnedBy(group, pool) && group.Spec.Revision == selected {
				needsConfig = needsConfig || groupUsesLiveInstanceAdmission(group)
			}
		}
		if !needsConfig {
			if !reflect.DeepEqual(pool.Spec.Template.InstanceAdmission, desired.InstanceAdmission) {
				return false, "DeploymentRequired", (&instanceAdmissionDeploymentRequiredError{poolName: desired.Name}).Error(), nil
			}
			for index := range groups.Items {
				group := &groups.Items[index]
				if routingGroupOwnedBy(group, pool) && group.Spec.Revision == selected && !reflect.DeepEqual(group.Spec.Runtime.InstanceAdmission, desired.InstanceAdmission) {
					return false, "DeploymentRequired", (&instanceAdmissionDeploymentRequiredError{poolName: desired.Name}).Error(), nil
				}
			}
			continue
		}
		configuration := new(corev1.ConfigMap)
		if err := reader.Get(ctx, client.ObjectKey{Namespace: pool.Namespace, Name: instanceAdmissionConfigMapName(string(pool.UID))}, configuration); apierrors.IsNotFound(err) {
			return pending()
		} else if err != nil {
			return false, "", "", err
		}
		if !metav1.IsControlledBy(configuration, pool) {
			return false, "ConfigurationRejected", "Instance admission ConfigMap is not controlled by its ModelPool", nil
		}
		config, err := readInstanceAdmissionConfiguration(configuration.Data[instanceAdmissionKey])
		if err != nil {
			return false, "ConfigurationRejected", fmt.Sprintf("Invalid instance admission configuration: %s", err), nil
		}
		if selected == "" || pool.Spec.DesiredGroups != desired.DesiredGroups || config.Version != pool.Status.InstanceAdmissionVersion || !reflect.DeepEqual(config.MaxConcurrentRequests, instanceAdmissionLimit(desired.InstanceAdmission)) {
			return pending()
		}
		observed := make(map[int32]bool)
		for index := range groups.Items {
			group := &groups.Items[index]
			if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != selected || group.Spec.Ordinal >= desired.DesiredGroups {
				continue
			}
			if !groupUsesLiveInstanceAdmission(group) || !routingGroupReady(group) || observed[group.Spec.Ordinal] {
				return pending()
			}
			// The stable leader Service uses the existing ingress port and network policy.
			endpoint := new(corev1.Service)
			if err := reader.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: modelGroupServiceName(group)}, endpoint); apierrors.IsNotFound(err) {
				return pending()
			} else if err != nil {
				return false, "", "", err
			}
			if !metav1.IsControlledBy(endpoint, group) || !reflect.DeepEqual(endpoint.Spec.Selector, modelGroupServiceSelector(group)) {
				return pending()
			}
			probeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			diagnostics, err := readInstanceAdmissionDiagnostics(probeCtx, modelGroupEndpoint(group, group.Spec.Runtime.Port))
			cancel()
			if err != nil || diagnostics.TargetGeneration == nil || *diagnostics.TargetGeneration != config.Version {
				return pending()
			}
			if diagnostics.ConfigurationError != nil {
				return false, "ConfigurationRejected", fmt.Sprintf("ModelGroup %q: %s", group.Name, *diagnostics.ConfigurationError), nil
			}
			if diagnostics.ActiveGeneration == nil || *diagnostics.ActiveGeneration != config.Version || !reflect.DeepEqual(diagnostics.MaxConcurrentRequests, config.MaxConcurrentRequests) || !diagnostics.Accepting {
				return pending()
			}
			observed[group.Spec.Ordinal] = true
		}
		if len(observed) != int(desired.DesiredGroups) {
			return pending()
		}
	}
	return true, "Ready", "Model instances have applied instance admission configuration", nil
}

// readInstanceAdmissionDiagnostics reads the configuration active at one Group ingress.
func readInstanceAdmissionDiagnostics(ctx context.Context, endpoint string) (instanceAdmissionDiagnostics, error) {
	var diagnostics instanceAdmissionDiagnostics
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/v1/internal/admission", nil)
	if err != nil {
		return diagnostics, err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return diagnostics, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return diagnostics, fmt.Errorf("instance admission returned HTTP %d", response.StatusCode)
	}
	if err := json.NewDecoder(response.Body).Decode(&diagnostics); err != nil {
		return diagnostics, fmt.Errorf("decode instance admission response: %w", err)
	}
	return diagnostics, nil
}
