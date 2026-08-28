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

const frontendAutoscalingTelemetryVersion = 1

type frontendAutoscalingTarget struct {
	ServiceUID     string `json:"service_uid"`
	TargetKind     string `json:"target_kind"`
	TargetID       string `json:"target_id"`
	QueuedRequests uint64 `json:"queued_requests"`
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

// HTTPPoolMetricsProvider reads every ready frontend replica and every currently
// routable model-server represented by one immutable scaling target.
type HTTPPoolMetricsProvider struct {
	client            client.Client
	httpClient        *http.Client
	collectionTimeout time.Duration
	concurrency       int
	now               func() time.Time
}

// NewHTTPPoolMetricsProvider constructs the frontend and model-server telemetry collector used by autoscaling.
func NewHTTPPoolMetricsProvider(kubeClient client.Client, options AutoscalingTelemetryOptions) *HTTPPoolMetricsProvider {
	return &HTTPPoolMetricsProvider{
		client:            kubeClient,
		httpClient:        &http.Client{Timeout: options.RequestTimeout},
		collectionTimeout: options.CollectionTimeout,
		concurrency:       options.Concurrency,
		now:               time.Now,
	}
}

// Observation aggregates fresh queue and active-request demand for one scaling target.
func (provider *HTTPPoolMetricsProvider) Observation(ctx context.Context, target core.TargetID) (core.DemandObservation, error) {
	if provider == nil || provider.client == nil || provider.httpClient == nil {
		return core.DemandObservation{}, fmt.Errorf("pool metrics provider is not configured")
	}
	startedAt := provider.now()
	collectionCtx, cancel := context.WithTimeout(ctx, provider.collectionTimeout)
	defer cancel()
	var queuedRequests, activeRequests uint64
	var queueSamples, activeSamples int64
	var queueObservedAt time.Time
	group, collectionCtx := errgroup.WithContext(collectionCtx)
	group.Go(func() error {
		var err error
		queuedRequests, queueSamples, queueObservedAt, err = provider.frontendQueue(collectionCtx, target)
		return err
	})
	group.Go(func() error {
		var err error
		activeRequests, activeSamples, err = provider.activeRequests(collectionCtx, target)
		return err
	})
	if err := group.Wait(); err != nil {
		return core.DemandObservation{}, err
	}
	collectedAt := provider.now()
	samples := queueSamples + activeSamples
	if samples > math.MaxInt32 {
		samples = math.MaxInt32
	}
	return core.DemandObservation{
		State: core.ObservationFresh,
		Window: core.ObservationWindow{
			Start:       startedAt,
			End:         queueObservedAt,
			CollectedAt: collectedAt,
			Samples:     int32(samples),
			Complete:    true,
		},
		QueueRequests:  saturatingInt64(queuedRequests),
		ActiveRequests: saturatingInt64(activeRequests),
	}, nil
}

// frontendQueue sums target-attributed queue samples from ready frontend Pods.
func (provider *HTTPPoolMetricsProvider) frontendQueue(ctx context.Context, target core.TargetID) (uint64, int64, time.Time, error) {
	var pods corev1.PodList
	if err := provider.client.List(ctx, &pods, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, time.Time{}, fmt.Errorf("list frontend Pods: %w", err)
	}
	type queueSample struct {
		queuedRequests uint64
		observedAt     time.Time
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
			return 0, 0, time.Time{}, err
		}
		name := pod.Name
		group.Go(func() error {
			telemetry, err := provider.getFrontendTelemetry(groupCtx, endpoint)
			if err != nil {
				return fmt.Errorf("frontend Pod %q autoscaling telemetry: %w", name, err)
			}
			queuedRequests, _ := telemetryTarget(telemetry.Targets, target)
			results <- queueSample{queuedRequests: queuedRequests, observedAt: time.UnixMilli(int64(telemetry.CollectedAtUnixMS))}
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return 0, 0, time.Time{}, err
	}
	close(results)
	var total uint64
	var samples int64
	var oldest time.Time
	for sample := range results {
		total = saturatingAdd(total, sample.queuedRequests)
		samples++
		if oldest.IsZero() || sample.observedAt.Before(oldest) {
			oldest = sample.observedAt
		}
	}
	if samples == 0 {
		return 0, 0, time.Time{}, fmt.Errorf("no ready frontend Pods")
	}
	return total, samples, oldest, nil
}

// activeRequests sums active requests from routable model servers for one target.
func (provider *HTTPPoolMetricsProvider) activeRequests(ctx context.Context, target core.TargetID) (uint64, int64, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := provider.client.Get(ctx, client.ObjectKey{Namespace: target.ServiceNamespace, Name: target.ServiceName}, service); err != nil {
		return 0, 0, fmt.Errorf("get ModelService for telemetry: %w", err)
	}
	if string(service.UID) != target.ServiceUID {
		return 0, 0, fmt.Errorf("ModelService UID changed for target %q", target.Name)
	}
	// Demand follows only the service-selected serving revision; preparing and draining
	// cohorts must not influence scaling. E/P/D aggregates all three stages as one target.
	var pools inferencev1alpha1.ModelPoolList
	if err := provider.client.List(ctx, &pools, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, fmt.Errorf("list ModelPools for telemetry: %w", err)
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
		return 0, 0, fmt.Errorf("no ModelPools found for target %q", target.Name)
	}

	var groups inferencev1alpha1.ModelGroupList
	if err := provider.client.List(ctx, &groups, client.InNamespace(target.ServiceNamespace)); err != nil {
		return 0, 0, fmt.Errorf("list ModelGroups for telemetry: %w", err)
	}
	results := make(chan uint64, len(groups.Items))
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
			results <- telemetry.RunningRequests
			return nil
		})
	}
	if err := requestGroup.Wait(); err != nil {
		return 0, 0, err
	}
	close(results)
	var total uint64
	var samples int64
	for value := range results {
		total = saturatingAdd(total, value)
		samples++
	}
	return total, samples, nil
}

// getFrontendTelemetry reads and validates one frontend autoscaling telemetry response.
func (provider *HTTPPoolMetricsProvider) getFrontendTelemetry(ctx context.Context, endpoint string) (frontendAutoscalingTelemetry, error) {
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
func (provider *HTTPPoolMetricsProvider) getModelTelemetry(ctx context.Context, endpoint string) (drainTelemetry, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/v1/internal/telemetry", nil)
	if err != nil {
		return drainTelemetry{}, err
	}
	response, err := provider.httpClient.Do(request)
	if err != nil {
		return drainTelemetry{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return drainTelemetry{}, fmt.Errorf("HTTP %d", response.StatusCode)
	}
	var telemetry drainTelemetry
	if err := json.NewDecoder(response.Body).Decode(&telemetry); err != nil {
		return drainTelemetry{}, fmt.Errorf("decode response: %w", err)
	}
	if telemetry.Version != modelServerTelemetryVersion {
		return drainTelemetry{}, fmt.Errorf("unsupported version %d", telemetry.Version)
	}
	return telemetry, nil
}

func telemetryTarget(values []frontendAutoscalingTarget, target core.TargetID) (uint64, bool) {
	targetID := target.UID
	if target.Kind == core.TargetEPDPipelineScope {
		targetID = target.ServiceUID
	}
	for _, value := range values {
		if value.ServiceUID == target.ServiceUID && value.TargetKind == string(target.Kind) && value.TargetID == targetID {
			return value.QueuedRequests, true
		}
	}
	return 0, false
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
