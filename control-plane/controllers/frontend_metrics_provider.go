// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Aggregates target-attributed frontend queue and model-server activity snapshots.

package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"golang.org/x/sync/errgroup"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const frontendAutoscalingTelemetryVersion = 2

type frontendAutoscalingTarget struct {
	ServiceUID             string `json:"service_uid"`
	TargetKind             string `json:"target_kind"`
	TargetID               string `json:"target_id"`
	RuntimeQueuedRequests  uint64 `json:"runtime_queued_requests"`
	DispatchQueuedRequests uint64 `json:"dispatch_queued_requests"`
}

type frontendAutoscalingTelemetry struct {
	Version           uint8                       `json:"version"`
	CollectedAtUnixMS uint64                      `json:"collected_at_unix_ms"`
	Targets           []frontendAutoscalingTarget `json:"targets"`
}

// AutoscalingTelemetryOptions defines the controller-wide collection budget.
type AutoscalingTelemetryOptions struct {
	CollectionTimeout time.Duration
	RequestTimeout    time.Duration
	Concurrency       int
}

// HTTPScalingMetricsProvider reads every ready frontend replica and every currently
// routable model-server represented by one immutable scaling target.
type HTTPScalingMetricsProvider struct {
	client            client.Client
	httpClient        *http.Client
	collectionTimeout time.Duration
	concurrency       int
	now               func() time.Time
}

// NewHTTPScalingMetricsProvider constructs the frontend and model-server telemetry collector used by autoscaling.
func NewHTTPScalingMetricsProvider(kubeClient client.Client, options AutoscalingTelemetryOptions) *HTTPScalingMetricsProvider {
	return &HTTPScalingMetricsProvider{
		client:            kubeClient,
		httpClient:        &http.Client{Timeout: options.RequestTimeout},
		collectionTimeout: options.CollectionTimeout,
		concurrency:       options.Concurrency,
		now:               time.Now,
	}
}

// Snapshot collects one complete backend-neutral metrics snapshot for a scaling target.
func (provider *HTTPScalingMetricsProvider) Snapshot(ctx context.Context, target core.TargetID) (core.MetricsSnapshot, error) {
	if provider == nil || provider.client == nil || provider.httpClient == nil {
		return core.MetricsSnapshot{}, fmt.Errorf("scaling metrics provider is not configured")
	}
	startedAt := provider.now()
	collectionCtx, cancel := context.WithTimeout(ctx, provider.collectionTimeout)
	defer cancel()
	var runtimeQueuedRequests, dispatchQueuedRequests, schedulerWaitingRequests, schedulerRunningRequests, activeRequests uint64
	var queueSamples, modelSamples int64
	var queueObservedAt, modelObservedAt time.Time
	group, collectionCtx := errgroup.WithContext(collectionCtx)
	group.Go(func() error {
		var err error
		runtimeQueuedRequests, dispatchQueuedRequests, queueSamples, queueObservedAt, err = provider.frontendQueue(collectionCtx, target)
		return err
	})
	group.Go(func() error {
		var err error
		schedulerWaitingRequests, schedulerRunningRequests, activeRequests, modelSamples, modelObservedAt, err = provider.modelDemand(collectionCtx, target)
		return err
	})
	if err := group.Wait(); err != nil {
		return core.MetricsSnapshot{}, err
	}
	collectedAt := provider.now()
	samples := queueSamples + modelSamples
	if samples > math.MaxInt32 {
		samples = math.MaxInt32
	}
	observedAt := queueObservedAt
	if !modelObservedAt.IsZero() && modelObservedAt.Before(observedAt) {
		observedAt = modelObservedAt
	}
	return core.MetricsSnapshot{
		State: core.MetricsFresh,
		Window: core.MetricsWindow{
			Start:       startedAt,
			End:         observedAt,
			CollectedAt: collectedAt,
			Samples:     int32(samples),
			Complete:    true,
		},
		// Runtime preparation is independent demand. Backend dispatch and scheduler queues can
		// observe the same request at adjacent stages, so count only their larger aggregate.
		WaitingRequests: saturatingInt64(saturatingAdd(
			runtimeQueuedRequests,
			max(dispatchQueuedRequests, schedulerWaitingRequests),
		)),
		RunningRequests: saturatingInt64(schedulerRunningRequests),
		ActiveRequests:  saturatingInt64(activeRequests),
	}, nil
}

// frontendQueue sums target-attributed queue samples from ready frontend Pods.
func (provider *HTTPScalingMetricsProvider) frontendQueue(ctx context.Context, target core.TargetID) (uint64, uint64, int64, time.Time, error) {
	var pods corev1.PodList
	if err := provider.client.List(ctx, &pods, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, 0, time.Time{}, fmt.Errorf("list frontend Pods: %w", err)
	}
	type queueSample struct {
		runtimeQueuedRequests  uint64
		dispatchQueuedRequests uint64
		observedAt             time.Time
	}
	results := make(chan queueSample, len(pods.Items))
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(provider.concurrency)
	for index := range pods.Items {
		pod := &pods.Items[index]
		if pod.Labels[frontendServiceLabel] == "" || !pod.DeletionTimestamp.IsZero() || !podReady(pod) {
			continue
		}
		endpoint, err := frontendPodEndpoint(pod)
		if err != nil {
			return 0, 0, 0, time.Time{}, err
		}
		name := pod.Name
		group.Go(func() error {
			telemetry, err := provider.getFrontendTelemetry(groupCtx, endpoint)
			if err != nil {
				return fmt.Errorf("frontend Pod %q autoscaling telemetry: %w", name, err)
			}
			value, _ := telemetryTarget(telemetry.Targets, target)
			results <- queueSample{
				runtimeQueuedRequests:  value.RuntimeQueuedRequests,
				dispatchQueuedRequests: value.DispatchQueuedRequests,
				observedAt:             time.UnixMilli(int64(telemetry.CollectedAtUnixMS)),
			}
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return 0, 0, 0, time.Time{}, err
	}
	close(results)
	var runtimeQueuedRequests, dispatchQueuedRequests uint64
	var samples int64
	var oldest time.Time
	for sample := range results {
		runtimeQueuedRequests = saturatingAdd(runtimeQueuedRequests, sample.runtimeQueuedRequests)
		dispatchQueuedRequests = saturatingAdd(dispatchQueuedRequests, sample.dispatchQueuedRequests)
		samples++
		if oldest.IsZero() || sample.observedAt.Before(oldest) {
			oldest = sample.observedAt
		}
	}
	if samples == 0 {
		return 0, 0, 0, time.Time{}, fmt.Errorf("no ready frontend Pods")
	}
	return runtimeQueuedRequests, dispatchQueuedRequests, samples, oldest, nil
}

// modelDemand sums scheduler backlog and admitted requests from routable model servers for one target.
func (provider *HTTPScalingMetricsProvider) modelDemand(ctx context.Context, target core.TargetID) (uint64, uint64, uint64, int64, time.Time, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := provider.client.Get(ctx, client.ObjectKey{Namespace: target.ServiceNamespace, Name: target.ServiceName}, service); err != nil {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("get ModelService for telemetry: %w", err)
	}
	if string(service.UID) != target.ServiceUID {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("ModelService UID changed for target %q", target.Name)
	}
	// Demand follows only the service-selected serving revision; preparing and draining
	// cohorts must not influence scaling. E/P/D aggregates all three stages as one target.
	var pools inferencev1alpha1.ModelPoolList
	if err := provider.client.List(ctx, &pools, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("list ModelPools for telemetry: %w", err)
	}
	selectedPools := make(map[string]*inferencev1alpha1.ModelPool)
	for index := range pools.Items {
		pool := &pools.Items[index]
		if !pool.DeletionTimestamp.IsZero() || pool.Spec.ModelServiceRef.UID != target.ServiceUID {
			continue
		}
		if target.Kind == core.TargetPool && string(pool.UID) != target.UID {
			continue
		}
		if target.Kind == core.TargetEPDPipelineScope && !isEPDRole(pool.Spec.Template.Role) {
			continue
		}
		selectedPools[string(pool.UID)] = pool
	}
	if len(selectedPools) == 0 {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("no ModelPools found for target %q", target.Name)
	}

	var groups inferencev1alpha1.ModelGroupList
	if err := provider.client.List(ctx, &groups, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("list ModelGroups for telemetry: %w", err)
	}
	type modelDemandSample struct {
		waitingRequests uint64
		runningRequests uint64
		activeRequests  uint64
		observedAt      time.Time
	}
	results := make(chan modelDemandSample, len(groups.Items))
	requestGroup, groupCtx := errgroup.WithContext(ctx)
	requestGroup.SetLimit(provider.concurrency)
	for index := range groups.Items {
		modelGroup := &groups.Items[index]
		pool := selectedPools[modelGroup.Spec.ModelPoolRef.UID]
		if pool == nil || !modelGroup.DeletionTimestamp.IsZero() || !modelGroupOwnedByPool(modelGroup, pool) || modelGroup.Spec.Revision != serviceServingRevision(service, pool) || !routingGroupReady(modelGroup) {
			continue
		}
		name := modelGroup.Name
		endpoint := modelGroupEndpoint(modelGroup, modelGroup.Spec.Runtime.Port)
		requestGroup.Go(func() error {
			telemetry, err := provider.getModelTelemetry(groupCtx, endpoint)
			if err != nil {
				return fmt.Errorf("ModelGroup %q telemetry: %w", name, err)
			}
			if !telemetry.Accepting {
				return fmt.Errorf("ModelGroup %q is not accepting", name)
			}
			if telemetry.CollectedAtUnixMS > math.MaxInt64 {
				return fmt.Errorf("ModelGroup %q telemetry has invalid collection timestamp", name)
			}
			if telemetry.SchedulerWaitingRequests == nil || telemetry.SchedulerRunningRequests == nil {
				return fmt.Errorf("ModelGroup %q telemetry has no scheduler request metrics", name)
			}
			results <- modelDemandSample{
				waitingRequests: *telemetry.SchedulerWaitingRequests,
				runningRequests: *telemetry.SchedulerRunningRequests,
				activeRequests:  telemetry.RunningRequests,
				observedAt:      time.UnixMilli(int64(telemetry.CollectedAtUnixMS)),
			}
			return nil
		})
	}
	if err := requestGroup.Wait(); err != nil {
		return 0, 0, 0, 0, time.Time{}, err
	}
	close(results)
	var waitingRequests, runningRequests, activeRequests uint64
	var samples int64
	var oldest time.Time
	for sample := range results {
		waitingRequests = saturatingAdd(waitingRequests, sample.waitingRequests)
		runningRequests = saturatingAdd(runningRequests, sample.runningRequests)
		activeRequests = saturatingAdd(activeRequests, sample.activeRequests)
		samples++
		if oldest.IsZero() || sample.observedAt.Before(oldest) {
			oldest = sample.observedAt
		}
	}
	if samples == 0 {
		return 0, 0, 0, 0, time.Time{}, fmt.Errorf("no routable ModelGroups for target %q", target.Name)
	}
	return waitingRequests, runningRequests, activeRequests, samples, oldest, nil
}

// getFrontendTelemetry reads and validates one frontend autoscaling telemetry response.
func (provider *HTTPScalingMetricsProvider) getFrontendTelemetry(ctx context.Context, endpoint string) (frontendAutoscalingTelemetry, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/internal/autoscaling/telemetry", nil)
	if err != nil {
		return frontendAutoscalingTelemetry{}, err
	}
	response, err := provider.httpClient.Do(request)
	if err != nil {
		return frontendAutoscalingTelemetry{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return frontendAutoscalingTelemetry{}, fmt.Errorf("HTTP %d", response.StatusCode)
	}
	var telemetry frontendAutoscalingTelemetry
	if err := json.NewDecoder(response.Body).Decode(&telemetry); err != nil {
		return frontendAutoscalingTelemetry{}, fmt.Errorf("decode response: %w", err)
	}
	if telemetry.Version != frontendAutoscalingTelemetryVersion {
		return frontendAutoscalingTelemetry{}, fmt.Errorf("unsupported version %d", telemetry.Version)
	}
	if telemetry.CollectedAtUnixMS > math.MaxInt64 {
		return frontendAutoscalingTelemetry{}, fmt.Errorf("invalid collection timestamp")
	}
	return telemetry, nil
}

// getModelTelemetry reads and validates one model-server telemetry response.
func (provider *HTTPScalingMetricsProvider) getModelTelemetry(ctx context.Context, endpoint string) (modelServerTelemetry, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/v1/internal/telemetry", nil)
	if err != nil {
		return modelServerTelemetry{}, err
	}
	response, err := provider.httpClient.Do(request)
	if err != nil {
		return modelServerTelemetry{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return modelServerTelemetry{}, fmt.Errorf("HTTP %d", response.StatusCode)
	}
	var telemetry modelServerTelemetry
	if err := json.NewDecoder(response.Body).Decode(&telemetry); err != nil {
		return modelServerTelemetry{}, fmt.Errorf("decode response: %w", err)
	}
	if telemetry.Version != modelServerTelemetryVersion {
		return modelServerTelemetry{}, fmt.Errorf("unsupported version %d", telemetry.Version)
	}
	return telemetry, nil
}

func telemetryTarget(values []frontendAutoscalingTarget, target core.TargetID) (frontendAutoscalingTarget, bool) {
	targetID := target.UID
	if target.Kind == core.TargetEPDPipelineScope {
		targetID = target.ServiceUID
	}
	for _, value := range values {
		if value.ServiceUID == target.ServiceUID && value.TargetKind == string(target.Kind) && value.TargetID == targetID {
			return value, true
		}
	}
	return frontendAutoscalingTarget{}, false
}

func saturatingAdd(left, right uint64) uint64 {
	if math.MaxUint64-left < right {
		return math.MaxUint64
	}
	return left + right
}

func saturatingInt64(value uint64) int64 {
	if value > math.MaxInt64 {
		return math.MaxInt64
	}
	return int64(value)
}
