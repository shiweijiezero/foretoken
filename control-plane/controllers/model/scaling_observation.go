// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Collects target-attributed frontend queues and model-server demand.

package model

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"
	"sync"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	"golang.org/x/sync/errgroup"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// schedulerEngineLoad explicitly decodes the versioned runtime token-demand contract.
// Pointers distinguish a valid idle zero from a missing required count.
type schedulerEngineLoad struct {
	DataParallelRank        *uint32 `json:"data_parallel_rank"`
	QueuedPrefillTokens     *uint64 `json:"queued_prefill_tokens"`
	ScheduledDecodeKVTokens *uint64 `json:"scheduled_decode_kv_tokens"`
	QueuedDecodeKVTokens    *uint64 `json:"queued_decode_kv_tokens"`
	ContextLength           uint64  `json:"context_length"`
	KVCapacityTokens        uint64  `json:"kv_capacity_tokens"`
}

const (
	frontendAutoscalingTelemetryVersion = 2
	modelGroupPoolUIDIndex              = ".spec.modelPoolRef.uid"
)

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

// AutoscalingTelemetryOptions defines the platform-owned budget copied for each observation.
type AutoscalingTelemetryOptions struct {
	CollectionTimeout metav1.Duration `json:"collectionTimeout"`
	RequestTimeout    metav1.Duration `json:"requestTimeout"`
	Concurrency       int             `json:"concurrency"`
}

// HTTPScalingMetricsProvider reads every ready frontend replica and every currently
// routable model-server represented by one immutable scaling target.
type HTTPScalingMetricsProvider struct {
	client             client.Client
	httpClient         *http.Client
	configurationPath  string
	optionsMu          sync.Mutex
	options            AutoscalingTelemetryOptions
	configurationError string
}

// modelDemandObservation retains one complete serving cohort for snapshot composition.
type modelDemandObservation struct {
	waitingRequests, runningRequests, activeRequests uint64
	engineLoads                                      []core.EngineLoad
	samples                                          int64
	observedAt                                       time.Time
}

// NewHTTPScalingMetricsProvider loads the platform file before starting the autoscaling collector.
func NewHTTPScalingMetricsProvider(kubeClient client.Client, configurationPath string) (*HTTPScalingMetricsProvider, error) {
	options, err := readAutoscalingTelemetryOptions(configurationPath)
	if err != nil {
		return nil, err
	}
	return &HTTPScalingMetricsProvider{
		client:            kubeClient,
		httpClient:        &http.Client{},
		configurationPath: configurationPath,
		options:           options,
	}, nil
}

// Snapshot collects one complete backend-neutral metrics snapshot for a scaling target.
func (provider *HTTPScalingMetricsProvider) Snapshot(ctx context.Context, target core.TargetID, needsEngineLoad bool) (core.MetricsSnapshot, error) {
	if provider == nil || provider.client == nil || provider.httpClient == nil {
		return core.MetricsSnapshot{}, fmt.Errorf("scaling metrics provider is not configured")
	}
	options := provider.copyOptions(ctx)
	startedAt := time.Now()
	collectionCtx, cancel := context.WithTimeout(ctx, options.CollectionTimeout.Duration)
	defer cancel()
	var runtimeQueuedRequests, dispatchQueuedRequests uint64
	var demand modelDemandObservation
	var queueSamples int64
	var queueObservedAt time.Time
	group, collectionCtx := errgroup.WithContext(collectionCtx)
	group.Go(func() error {
		var err error
		runtimeQueuedRequests, dispatchQueuedRequests, queueSamples, queueObservedAt, err = provider.frontendQueue(collectionCtx, target, options)
		return err
	})
	group.Go(func() error {
		var err error
		demand, err = provider.modelDemand(collectionCtx, target, options, needsEngineLoad)
		return err
	})
	if err := group.Wait(); err != nil {
		return core.MetricsSnapshot{}, err
	}
	collectedAt := time.Now()
	samples := queueSamples + demand.samples
	if samples > math.MaxInt32 {
		samples = math.MaxInt32
	}
	observedAt := queueObservedAt
	if !demand.observedAt.IsZero() && demand.observedAt.Before(observedAt) {
		observedAt = demand.observedAt
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
			max(dispatchQueuedRequests, demand.waitingRequests),
		)),
		RunningRequests: saturatingInt64(demand.runningRequests),
		ActiveRequests:  saturatingInt64(demand.activeRequests),
		EngineLoads:     demand.engineLoads,
	}, nil
}

// getSchedulerLoad reads every rank in one Group only when its policy needs token demand.
// The collector pins attribution to the selected Group and rejects incomplete observations.
func (provider *HTTPScalingMetricsProvider) getSchedulerLoad(ctx context.Context, endpoint string, timeout time.Duration, group *inferencev1alpha1.ModelGroup) (modelDemandObservation, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/v1/internal/scheduler-load", nil)
	if err != nil {
		return modelDemandObservation{}, err
	}
	response, err := provider.httpClient.Do(request)
	if err != nil {
		return modelDemandObservation{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return modelDemandObservation{}, fmt.Errorf("HTTP %d", response.StatusCode)
	}
	var payload struct {
		Version           uint8                 `json:"version"`
		CollectedAtUnixMS uint64                `json:"collected_at_unix_ms"`
		Engines           []schedulerEngineLoad `json:"engines"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return modelDemandObservation{}, fmt.Errorf("decode scheduler load: %w", err)
	}
	if payload.Version != 1 || payload.CollectedAtUnixMS == 0 || payload.CollectedAtUnixMS > math.MaxInt64 || len(payload.Engines) != int(group.Spec.Parallelism.DP) {
		return modelDemandObservation{}, fmt.Errorf("scheduler load has invalid version, timestamp, or rank coverage")
	}
	observation := modelDemandObservation{observedAt: time.UnixMilli(int64(payload.CollectedAtUnixMS))}
	seen := make(map[uint32]bool, len(payload.Engines))
	for _, engine := range payload.Engines {
		if engine.DataParallelRank == nil || engine.QueuedPrefillTokens == nil || engine.ScheduledDecodeKVTokens == nil || engine.QueuedDecodeKVTokens == nil || engine.ContextLength == 0 || engine.KVCapacityTokens == 0 {
			return modelDemandObservation{}, fmt.Errorf("scheduler load is missing required token counts or capacities")
		}
		rank := *engine.DataParallelRank
		if rank >= uint32(group.Spec.Parallelism.DP) || seen[rank] {
			return modelDemandObservation{}, fmt.Errorf("scheduler load has duplicate or unexpected DP rank %d", rank)
		}
		seen[rank] = true
		observation.engineLoads = append(observation.engineLoads, core.EngineLoad{
			GroupUID: string(group.UID), DataParallelRank: rank,
			QueuedPrefillTokens:     *engine.QueuedPrefillTokens,
			ScheduledDecodeKVTokens: *engine.ScheduledDecodeKVTokens,
			QueuedDecodeKVTokens:    *engine.QueuedDecodeKVTokens,
			ContextLength:           engine.ContextLength, KVCapacityTokens: engine.KVCapacityTokens,
		})
	}
	return observation, nil
}

// readAutoscalingTelemetryOptions decodes the complete platform file for startup and later observations.
func readAutoscalingTelemetryOptions(path string) (AutoscalingTelemetryOptions, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return AutoscalingTelemetryOptions{}, fmt.Errorf("read autoscaling telemetry configuration %q: %w", path, err)
	}
	var options AutoscalingTelemetryOptions
	if err := json.Unmarshal(data, &options); err != nil {
		return AutoscalingTelemetryOptions{}, fmt.Errorf("decode autoscaling telemetry configuration %q: %w", path, err)
	}
	if options.CollectionTimeout.Duration <= 0 {
		return AutoscalingTelemetryOptions{}, fmt.Errorf("autoscaling telemetry collectionTimeout must be positive")
	}
	if options.RequestTimeout.Duration <= 0 {
		return AutoscalingTelemetryOptions{}, fmt.Errorf("autoscaling telemetry requestTimeout must be positive")
	}
	if options.Concurrency < 1 {
		return AutoscalingTelemetryOptions{}, fmt.Errorf("autoscaling telemetry concurrency must be positive")
	}
	return options, nil
}

// copyOptions observes projected file updates without changing settings of in-flight collections.
func (provider *HTTPScalingMetricsProvider) copyOptions(ctx context.Context) AutoscalingTelemetryOptions {
	provider.optionsMu.Lock()
	defer provider.optionsMu.Unlock()
	options, err := readAutoscalingTelemetryOptions(provider.configurationPath)
	if err != nil {
		if provider.configurationError != err.Error() {
			ctrl.LoggerFrom(ctx).Error(err, "retaining the last valid autoscaling telemetry configuration")
			provider.configurationError = err.Error()
		}
		return provider.options
	}
	if options != provider.options || provider.configurationError != "" {
		ctrl.LoggerFrom(ctx).Info("loaded autoscaling telemetry configuration", "collectionTimeout", options.CollectionTimeout.Duration.String(), "requestTimeout", options.RequestTimeout.Duration.String(), "concurrency", options.Concurrency)
	}
	provider.options = options
	provider.configurationError = ""
	return options
}

// frontendQueue sums target-attributed queue samples from ready frontend Pods.
func (provider *HTTPScalingMetricsProvider) frontendQueue(ctx context.Context, target core.TargetID, options AutoscalingTelemetryOptions) (uint64, uint64, int64, time.Time, error) {
	var pods corev1.PodList
	if err := provider.client.List(ctx, &pods, client.InNamespace(target.ServiceNamespace), client.HasLabels{workload.FrontendServiceLabel}); err != nil {
		return 0, 0, 0, time.Time{}, fmt.Errorf("list frontend Pods: %w", err)
	}
	type queueSample struct {
		runtimeQueuedRequests  uint64
		dispatchQueuedRequests uint64
		observedAt             time.Time
	}
	results := make(chan queueSample, len(pods.Items))
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(options.Concurrency)
	for index := range pods.Items {
		pod := &pods.Items[index]
		if pod.Labels[workload.FrontendServiceLabel] == "" || !pod.DeletionTimestamp.IsZero() || !workload.PodReady(pod) {
			continue
		}
		endpoint, err := workload.FrontendPodEndpoint(pod)
		if err != nil {
			return 0, 0, 0, time.Time{}, err
		}
		name := pod.Name
		group.Go(func() error {
			telemetry, err := provider.getFrontendTelemetry(groupCtx, endpoint, options.RequestTimeout.Duration)
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
func (provider *HTTPScalingMetricsProvider) modelDemand(ctx context.Context, target core.TargetID, options AutoscalingTelemetryOptions, needsEngineLoad bool) (modelDemandObservation, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := provider.client.Get(ctx, client.ObjectKey{Namespace: target.ServiceNamespace, Name: target.ServiceName}, service); err != nil {
		return modelDemandObservation{}, fmt.Errorf("get ModelService for telemetry: %w", err)
	}
	if string(service.UID) != target.ServiceUID {
		return modelDemandObservation{}, fmt.Errorf("ModelService UID changed for target %q", target.Name)
	}
	// Demand follows only the service-selected serving revision; preparing and draining
	// cohorts must not influence the Pool's scaling decision.
	var pools inferencev1alpha1.ModelPoolList
	if err := provider.client.List(ctx, &pools, client.InNamespace(target.ServiceNamespace)); err != nil {
		return modelDemandObservation{}, fmt.Errorf("list ModelPools for telemetry: %w", err)
	}
	var selectedPool *inferencev1alpha1.ModelPool
	for index := range pools.Items {
		pool := &pools.Items[index]
		if pool.DeletionTimestamp.IsZero() && pool.Spec.ModelServiceRef.UID == target.ServiceUID && string(pool.UID) == target.UID {
			selectedPool = pool
			break
		}
	}
	if selectedPool == nil {
		return modelDemandObservation{}, fmt.Errorf("no ModelPool found for target %q", target.Name)
	}

	var groups inferencev1alpha1.ModelGroupList
	if err := provider.client.List(ctx, &groups, client.InNamespace(target.ServiceNamespace), client.MatchingFields{modelGroupPoolUIDIndex: target.UID}); err != nil {
		return modelDemandObservation{}, fmt.Errorf("list ModelGroups for telemetry: %w", err)
	}
	type modelDemandSample struct {
		waitingRequests uint64
		runningRequests uint64
		activeRequests  uint64
		engineLoads     []core.EngineLoad
		observedAt      time.Time
	}
	results := make(chan modelDemandSample, len(groups.Items))
	requestGroup, groupCtx := errgroup.WithContext(ctx)
	requestGroup.SetLimit(options.Concurrency)
	for index := range groups.Items {
		modelGroup := &groups.Items[index]
		if !modelGroup.DeletionTimestamp.IsZero() || !servingconfig.ModelGroupOwnedBy(modelGroup, selectedPool) || modelGroup.Spec.Revision != servingconfig.ServingRevision(service, selectedPool) || !servingconfig.RoutingGroupReady(modelGroup) {
			continue
		}
		name := modelGroup.Name
		endpoint := workload.ModelGroupEndpoint(modelGroup, modelGroup.Spec.Runtime.Port)
		requestGroup.Go(func() error {
			telemetry, err := provider.getModelTelemetry(groupCtx, endpoint, options.RequestTimeout.Duration)
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
			var loads []core.EngineLoad
			observedAt := time.UnixMilli(int64(telemetry.CollectedAtUnixMS))
			if needsEngineLoad {
				load, err := provider.getSchedulerLoad(groupCtx, endpoint, options.RequestTimeout.Duration, modelGroup)
				if err != nil {
					return fmt.Errorf("ModelGroup %q scheduler load: %w", name, err)
				}
				loads = load.engineLoads
				if load.observedAt.Before(observedAt) {
					observedAt = load.observedAt
				}
			}
			results <- modelDemandSample{
				waitingRequests: *telemetry.SchedulerWaitingRequests,
				runningRequests: *telemetry.SchedulerRunningRequests,
				activeRequests:  telemetry.RunningRequests,
				engineLoads:     loads,
				observedAt:      observedAt,
			}
			return nil
		})
	}
	if err := requestGroup.Wait(); err != nil {
		return modelDemandObservation{}, err
	}
	close(results)
	var observation modelDemandObservation
	for sample := range results {
		observation.waitingRequests = saturatingAdd(observation.waitingRequests, sample.waitingRequests)
		observation.runningRequests = saturatingAdd(observation.runningRequests, sample.runningRequests)
		observation.activeRequests = saturatingAdd(observation.activeRequests, sample.activeRequests)
		observation.engineLoads = append(observation.engineLoads, sample.engineLoads...)
		observation.samples++
		if observation.observedAt.IsZero() || sample.observedAt.Before(observation.observedAt) {
			observation.observedAt = sample.observedAt
		}
	}
	if observation.samples == 0 {
		return modelDemandObservation{}, fmt.Errorf("no routable ModelGroups for target %q", target.Name)
	}
	return observation, nil
}

// getFrontendTelemetry reads and validates one frontend autoscaling telemetry response.
func (provider *HTTPScalingMetricsProvider) getFrontendTelemetry(ctx context.Context, endpoint string, timeout time.Duration) (frontendAutoscalingTelemetry, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
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
func (provider *HTTPScalingMetricsProvider) getModelTelemetry(ctx context.Context, endpoint string, timeout time.Duration) (modelServerTelemetry, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
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
	if err := telemetry.validateVersion(); err != nil {
		return modelServerTelemetry{}, err
	}
	return telemetry, nil
}

func telemetryTarget(values []frontendAutoscalingTarget, target core.TargetID) (frontendAutoscalingTarget, bool) {
	for _, value := range values {
		if value.ServiceUID == target.ServiceUID && value.TargetKind == string(target.Kind) && value.TargetID == target.UID {
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
