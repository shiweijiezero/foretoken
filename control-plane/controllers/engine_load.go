// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Collects complete per-rank token demand for a model Group.
package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
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

// getSchedulerLoad reads every rank in one Group only when its policy needs token demand.
// The collector pins attribution to the selected Group and rejects incomplete observations.
func (provider *HTTPScalingMetricsProvider) getSchedulerLoad(ctx context.Context, endpoint string, timeout time.Duration, group *api.ModelGroup) (modelDemandObservation, error) {
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
