// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes versioned frontend configuration and refreshes its actual consumers.

package frontend

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
)

var errFrontendCacheIsolationUnsupported = errors.New("redeploy the frontend with the current application to use prefixCacheIsolation")

// servingSnapshotResult separates rejected configuration from publication or observation failures.
type servingSnapshotResult struct {
	Published          bool
	ConfigurationError error
}

// reconcileServingSnapshot publishes routing, settings, and membership at one version boundary.
// Rejected intent can retain membership updates; runtime errors return to the controller's retry owner.
func (reconciler *Reconciler) reconcileServingSnapshot(ctx context.Context, frontend *inferencev1alpha1.FrontendService, services []inferencev1alpha1.ModelService, preserveConfiguration bool) (servingSnapshotResult, error) {
	name := servingconfig.ConfigMapName(frontend, frontend.Status.ServingConfigVersion)
	protocols := servingconfig.ProtocolVersions(frontend)
	type servingPublication struct {
		snapshot  servingconfig.Snapshot
		configMap *corev1.ConfigMap
	}
	publications := make(map[uint32]servingPublication, len(protocols))
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var previous servingconfig.Snapshot
	var primaryVersion uint64
	previousFound, previousValid := false, false
	for index, protocol := range protocols {
		publicationName := servingconfig.ConfigMapName(frontend, protocol)
		current := new(corev1.ConfigMap)
		if err := reader.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: publicationName}, current); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return servingSnapshotResult{}, fmt.Errorf("get serving snapshot ConfigMap: %w", err)
		}
		if !metav1.IsControlledBy(current, frontend) {
			return servingSnapshotResult{}, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", publicationName)
		}
		snapshot, valid, err := readServingPublication([]byte(current.Data[servingconfig.SnapshotKey]))
		if err != nil {
			return servingSnapshotResult{}, fmt.Errorf("decode published serving snapshot: %w", err)
		}
		// A partial publication can leave the legacy view with newer cohort and member
		// provenance. At equal versions, retain the primary view's complete routes.
		if !previousFound || snapshot.Version > previous.Version {
			previous, previousValid, previousFound = snapshot, valid, true
		}
		publications[protocol] = servingPublication{snapshot: snapshot, configMap: current}
		if index == 0 {
			primaryVersion = snapshot.Version
		}
	}
	if preserveConfiguration && (!previousValid || frontend.Status.ServingConfigVersion < servingconfig.AdmissionProtocol) {
		return servingSnapshotResult{}, nil
	}
	instancePods, err := reconciler.frontendInstancePods(ctx, frontend, previous.FrontendInstances)
	if err != nil {
		return servingSnapshotResult{}, err
	}
	frontendInstances := make([]string, 0, len(instancePods))
	consumedViews := make(map[string]bool)
	for _, pod := range instancePods {
		frontendInstances = append(frontendInstances, string(pod.UID))
		if pod.DeletionTimestamp.IsZero() {
			consumedViews[servingconfig.PodConfigMapName(&pod)] = true
		}
	}
	slices.Sort(frontendInstances)
	// Recreated mounts need a fresh acknowledgement even when their projected contents are unchanged.
	missingConsumedView := slices.ContainsFunc(protocols, func(protocol uint32) bool {
		_, exists := publications[protocol]
		return !exists && consumedViews[servingconfig.ConfigMapName(frontend, protocol)]
	})
	backendInstances, err := reconciler.backendInstances(ctx, frontend.Namespace, previous.BackendInstances)
	if err != nil {
		return servingSnapshotResult{}, err
	}
	configuration := previous
	if primary, exists := publications[frontend.Status.ServingConfigVersion]; preserveConfiguration && exists {
		configuration = primary.snapshot
	}
	settings, models, admission := configuration.Settings, configuration.Models, configuration.Admission
	groups, pdComponents, pdPipelineScopes := configuration.Groups, configuration.PDComponents, configuration.PDPipelineScopes
	epdComponents, epdPipelineScopes := configuration.EPDComponents, configuration.EPDPipelineScopes
	var projectionErr error
	if !preserveConfiguration {
		requestTimeout, err := runtimeconfig.WholeDurationSeconds(frontend.Spec.Timeouts.Request)
		if err != nil {
			return servingSnapshotResult{ConfigurationError: fmt.Errorf("parse request timeout: %w", err)}, nil
		}
		streamIdle, err := runtimeconfig.WholeDurationSeconds(frontend.Spec.Timeouts.StreamIdle)
		if err != nil {
			return servingSnapshotResult{ConfigurationError: fmt.Errorf("parse stream idle timeout: %w", err)}, nil
		}
		settings = servingconfig.Settings{
			RouterPipeline:        frontend.Spec.RouterPipeline,
			RequestTimeoutSeconds: requestTimeout,
			StreamIdleSeconds:     streamIdle,
			LogLevel:              frontend.Spec.LogLevel,
		}
		// Catalog and routes use the same observed resources for this publication.
		var pools inferencev1alpha1.ModelPoolList
		if err := reconciler.List(ctx, &pools, client.InNamespace(frontend.Namespace)); err != nil {
			return servingSnapshotResult{}, fmt.Errorf("list ModelPools for model catalog: %w", err)
		}
		var modelGroups inferencev1alpha1.ModelGroupList
		if err := reconciler.List(ctx, &modelGroups, client.InNamespace(frontend.Namespace)); err != nil {
			return servingSnapshotResult{}, fmt.Errorf("list ModelGroups for model catalog: %w", err)
		}
		var catalogErr error
		models, admission, catalogErr = projectConfiguredModels(frontend, services, pools.Items, modelGroups.Items, previous.Models)
		if catalogErr != nil {
			var unavailable *servingconfig.ModelCatalogProjectionError
			if !errors.As(catalogErr, &unavailable) {
				return servingSnapshotResult{ConfigurationError: catalogErr}, nil
			}
		}
		// A service without a proven catalog identity cannot contribute executable routes.
		routingServices := make([]inferencev1alpha1.ModelService, 0, len(models))
		for _, service := range services {
			if slices.ContainsFunc(models, func(model servingconfig.Model) bool { return model.ServiceUID == string(service.UID) }) {
				routingServices = append(routingServices, service)
			}
		}
		groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes, projectionErr = projectableRouting(routingServices, pools.Items, modelGroups.Items)
		if projectionErr != nil {
			var splitProjectionError *servingconfig.SplitRoutingProjectionError
			if !errors.As(projectionErr, &splitProjectionError) {
				return servingSnapshotResult{ConfigurationError: projectionErr}, nil
			}
			// Service-local split failures have already been excluded from the partial projection.
		}
		if err := servingconfig.ValidateRoutingIdentities(models, groups, pdComponents, epdComponents); err != nil {
			return servingSnapshotResult{ConfigurationError: err}, nil
		}
		projectionErr = errors.Join(catalogErr, projectionErr)
	}

	snapshot := servingconfig.Snapshot{Settings: settings, FrontendInstances: frontendInstances, BackendInstances: backendInstances, Models: models, Admission: admission, Groups: groups, PDComponents: pdComponents, PDPipelineScopes: pdPipelineScopes, EPDComponents: epdComponents, EPDPipelineScopes: epdPipelineScopes}
	if frontend.Status.ServingConfigVersion < servingconfig.CacheIsolationProtocol && !preserveConfiguration && slices.ContainsFunc(models, func(model servingconfig.Model) bool { return model.PrefixCacheIsolation != nil }) {
		projectionErr = errors.Join(projectionErr, errFrontendCacheIsolationUnsupported)
	}

	snapshot = servingSnapshotForProtocol(snapshot, frontend.Status.ServingConfigVersion)
	// A restored payload below the durable floor cannot prove what consumers applied
	// at that version. Allocate a fresh version even when it matches current intent.
	version := max(frontend.Status.ServingSnapshotVersion, previous.Version)
	contentsChanged := true
	if previousValid {
		previousView := servingSnapshotForProtocol(previous, frontend.Status.ServingConfigVersion)
		contentsChanged = !previousView.SameConfiguration(snapshot)
	}
	if contentsChanged || missingConsumedView || primaryVersion < version || version == 0 {
		if version == ^uint64(0) {
			return servingSnapshotResult{}, fmt.Errorf("serving snapshot publication version is exhausted")
		}
		version++
	}
	snapshot.Version = version
	// Reserve the version before it becomes visible to consumers, including across
	// a controller restart between publication and the later readiness observation.
	if frontend.Status.ServingSnapshotVersion < version {
		base := frontend.DeepCopy()
		frontend.Status.ServingSnapshotVersion = version
		// Retain an existing failure until publication and acknowledgement recover.
		for _, condition := range []string{servingconfig.ConditionRoutingReady, workload.ConditionReady} {
			if !meta.IsStatusConditionFalse(frontend.Status.Conditions, condition) {
				setFrontendCondition(frontend, condition, metav1.ConditionFalse, "ConfigurationPending", "Waiting for frontend replicas to apply the serving configuration")
			}
		}
		if err := reconciler.Status().Patch(ctx, frontend, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return servingSnapshotResult{}, fmt.Errorf("reserve serving snapshot version: %w", err)
		}
	}
	// Protocol-specific routes share one version and identical ledger inputs. Updating the
	// legacy mount first also keeps withdrawal acknowledgements valid across a rolling update.
	for _, protocol := range protocols[1:] {
		publication, exists := publications[protocol]
		if !exists && !consumedViews[servingconfig.ConfigMapName(frontend, protocol)] {
			continue
		}
		view := snapshot
		if preserveConfiguration && exists {
			view = publication.snapshot
			view.Version = snapshot.Version
			view.FrontendInstances = frontendInstances
			view.BackendInstances = backendInstances
		}
		if err := reconciler.publishServingSnapshot(ctx, frontend, servingconfig.ConfigMapName(frontend, protocol), servingSnapshotForProtocol(view, protocol), publication.configMap); err != nil {
			return servingSnapshotResult{}, err
		}
	}
	if err := reconciler.publishServingSnapshot(ctx, frontend, name, snapshot, publications[frontend.Status.ServingConfigVersion].configMap); err != nil {
		return servingSnapshotResult{}, err
	}
	// Updating Pod metadata prompts kubelet to refresh the projected ConfigMap.
	// Consumer acknowledgements still come from the frontend's active generation.
	const refreshAnnotation = "inference.foretoken.io/serving-config-version"
	refreshVersion := fmt.Sprint(version)
	for index := range instancePods {
		pod := &instancePods[index]
		if !pod.DeletionTimestamp.IsZero() || pod.Annotations[refreshAnnotation] == refreshVersion {
			continue
		}
		base := pod.DeepCopy()
		if pod.Annotations == nil {
			pod.Annotations = make(map[string]string)
		}
		pod.Annotations[refreshAnnotation] = refreshVersion
		if err := reconciler.Patch(ctx, pod, client.MergeFrom(base)); err != nil && !apierrors.IsNotFound(err) {
			return servingSnapshotResult{Published: true, ConfigurationError: projectionErr}, fmt.Errorf("refresh frontend Pod %q config: %w", pod.Name, err)
		}
	}
	return servingSnapshotResult{Published: true, ConfigurationError: projectionErr}, nil
}

// servingSnapshotForProtocol projects only settings and routes understood by the selected application.
// All protocol views retain the same publication version and admission membership.
func servingSnapshotForProtocol(snapshot servingconfig.Snapshot, protocol uint32) servingconfig.Snapshot {
	if protocol < servingconfig.ModelRoutingProtocol {
		snapshot.Models = slices.Clone(snapshot.Models)
		for index := range snapshot.Models {
			snapshot.Models[index].RouterPipeline = nil
		}
	}
	if protocol < servingconfig.CacheIsolationProtocol {
		snapshot = admissionOnlyServingSnapshot(snapshot)
	}
	return snapshot
}

// admissionOnlyServingSnapshot excludes routes whose cache policy an admission-only application cannot enforce.
// Catalog provenance and ledger inputs stay identical to the current protocol's publication.
func admissionOnlyServingSnapshot(snapshot servingconfig.Snapshot) servingconfig.Snapshot {
	snapshot.Groups = slices.DeleteFunc(slices.Clone(snapshot.Groups), func(group servingconfig.Group) bool {
		return group.PrefixCacheIsolation != nil
	})
	blocked := make(map[string]bool)
	snapshot.PDComponents = slices.DeleteFunc(slices.Clone(snapshot.PDComponents), func(component servingconfig.PDComponent) bool {
		if component.PrefixCacheIsolation == nil {
			return false
		}
		blocked[component.RouteTargetID] = true
		return true
	})
	snapshot.EPDComponents = slices.DeleteFunc(slices.Clone(snapshot.EPDComponents), func(component servingconfig.EPDComponent) bool {
		if component.PrefixCacheIsolation == nil {
			return false
		}
		blocked[component.RouteTargetID] = true
		return true
	})
	isBlocked := func(target string) bool { return blocked[target] }
	snapshot.PDPipelineScopes = slices.DeleteFunc(slices.Clone(snapshot.PDPipelineScopes), func(scope servingconfig.PDPipelineScope) bool {
		return slices.ContainsFunc(scope.PrefillRouteTargetIDs, isBlocked) || slices.ContainsFunc(scope.DecodeRouteTargetIDs, isBlocked)
	})
	snapshot.EPDPipelineScopes = slices.DeleteFunc(slices.Clone(snapshot.EPDPipelineScopes), func(scope servingconfig.EPDPipelineScope) bool {
		return slices.ContainsFunc(scope.EncoderRouteTargetIDs, isBlocked) || slices.ContainsFunc(scope.PrefillRouteTargetIDs, isBlocked) || slices.ContainsFunc(scope.DecodeRouteTargetIDs, isBlocked)
	})
	return snapshot
}

// publishServingSnapshot writes one protocol view against the object read for this candidate.
// The FrontendService owns each view; a deleted or concurrently changed view must be reconciled again.
func (reconciler *Reconciler) publishServingSnapshot(ctx context.Context, frontend *inferencev1alpha1.FrontendService, name string, snapshot servingconfig.Snapshot, current *corev1.ConfigMap) error {
	payload, err := json.Marshal(snapshot)
	if err != nil {
		return fmt.Errorf("encode routing snapshot: %w", err)
	}
	if current == nil {
		desired := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: frontend.Namespace,
				Labels:    map[string]string{workload.FrontendServiceLabel: frontend.Name},
			},
			Data: map[string]string{servingconfig.SnapshotKey: string(payload)},
		}
		if err := controllerutil.SetControllerReference(frontend, desired, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set serving snapshot ConfigMap owner: %w", err)
		}
		if err := reconciler.Create(ctx, desired); err != nil {
			return fmt.Errorf("create serving snapshot ConfigMap: %w", err)
		}
		return nil
	}
	if current.Data[servingconfig.SnapshotKey] == string(payload) && current.Labels[workload.FrontendServiceLabel] == frontend.Name {
		return nil
	}
	updated := current.DeepCopy()
	if updated.Data == nil {
		updated.Data = make(map[string]string)
	}
	updated.Data[servingconfig.SnapshotKey] = string(payload)
	if updated.Labels == nil {
		updated.Labels = make(map[string]string)
	}
	updated.Labels[workload.FrontendServiceLabel] = frontend.Name
	// The informer can deliver before Update returns. Hold receipt ownership
	// across the write so its echo cannot trigger another partial publication.
	reconciler.snapshotWriteMu.Lock()
	defer reconciler.snapshotWriteMu.Unlock()
	// Retain UID and resourceVersion so deletion cannot silently recreate a view at an already-acknowledged version.
	if err := reconciler.Update(ctx, updated); err != nil {
		return fmt.Errorf("update serving snapshot ConfigMap: %w", err)
	}
	if reconciler.snapshotWrites == nil {
		reconciler.snapshotWrites = make(map[types.UID][]string)
	}
	reconciler.snapshotWrites[updated.UID] = append(reconciler.snapshotWrites[updated.UID], updated.ResourceVersion)
	return nil
}

// snapshotInputChanged consumes this publisher's exact write receipts and preserves external changes.
// Informer delivery can skip intermediate versions; their earlier receipts then retire together.
func (reconciler *Reconciler) snapshotInputChanged(update event.UpdateEvent) bool {
	reconciler.snapshotWriteMu.Lock()
	defer reconciler.snapshotWriteMu.Unlock()
	uid := update.ObjectNew.GetUID()
	versions := reconciler.snapshotWrites[uid]
	index := slices.Index(versions, update.ObjectNew.GetResourceVersion())
	if index < 0 {
		return true
	}
	if index+1 == len(versions) {
		delete(reconciler.snapshotWrites, uid)
	} else {
		reconciler.snapshotWrites[uid] = versions[index+1:]
	}
	return false
}

// readServingPublication separates admission-schema migration from published identity provenance.
// Unsupported admission fields force a new version without discarding model or instance ownership.
func readServingPublication(data []byte) (servingconfig.Snapshot, bool, error) {
	type publication servingconfig.Snapshot
	var previous servingconfig.Snapshot
	decoded := struct {
		*publication
		Admission json.RawMessage `json:"admission"`
	}{publication: (*publication)(&previous)}
	if err := json.Unmarshal(data, &decoded); err != nil {
		return servingconfig.Snapshot{}, false, err
	}
	var admission map[string]json.RawMessage
	if err := json.Unmarshal(decoded.Admission, &admission); err != nil {
		return previous, false, nil
	}
	previous.Admission = make(map[string]servingconfig.Admission, len(admission))
	valid := true
	for model, raw := range admission {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(raw, &fields); err != nil {
			valid = false
			continue
		}
		for name := range fields {
			if name != "maxWaitingRequests" && name != "queueTimeout" && name != "roleRules" {
				valid = false
			}
		}
		var config servingconfig.Admission
		if err := json.Unmarshal(raw, &config); err != nil {
			valid = false
			continue
		}
		previous.Admission[model] = config
	}
	return previous, valid, nil
}
