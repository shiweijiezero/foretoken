// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import (
	"sync"
	"time"
)

type AdjustmentReason string

const (
	AdjustmentReasonDirect              AdjustmentReason = "Direct"
	AdjustmentReasonStepUp              AdjustmentReason = "StepUp"
	AdjustmentReasonStepDown            AdjustmentReason = "StepDown"
	AdjustmentReasonScaleUpLimited      AdjustmentReason = "ScaleUpLimited"
	AdjustmentReasonScaleDownLimited    AdjustmentReason = "ScaleDownLimited"
	AdjustmentReasonScaleUpStabilized   AdjustmentReason = "ScaleUpStabilized"
	AdjustmentReasonScaleDownStabilized AdjustmentReason = "ScaleDownStabilized"
	AdjustmentReasonHold                AdjustmentReason = "Hold"
)

type AdjustmentInput struct {
	Target        TargetID
	EvaluatedAt   time.Time
	CurrentGroups int32
	DesiredGroups int32
	Bounds        CapacityLimits
}

type ScalingAdjustment struct {
	AdjustedGroups int32
	Reason         AdjustmentReason
	Message        string
}

type AdjustmentConfig struct {
	MaxScaleUpGroups             int32
	MaxScaleDownGroups           int32
	ScaleUpStabilizationWindow   time.Duration
	ScaleDownStabilizationWindow time.Duration
	History                      *RecommendationHistory
}

type AdjustmentAlgorithm interface {
	Name() string
	Adjust(AdjustmentInput) (ScalingAdjustment, error)
}

type capacityRecommendation struct {
	at     time.Time
	groups int32
}

// RecommendationHistory retains recent desired capacities used by stabilization windows.
type RecommendationHistory struct {
	mu      sync.Mutex
	targets map[TargetID][]capacityRecommendation
}

// NewRecommendationHistory creates empty in-memory recommendation history for one controller process.
func NewRecommendationHistory() *RecommendationHistory {
	return &RecommendationHistory{targets: make(map[TargetID][]capacityRecommendation)}
}

// Stabilize records one desired capacity and returns the conservative recommendation for its direction.
func (history *RecommendationHistory) Stabilize(target TargetID, now time.Time, current, desired int32, scaleUpWindow, scaleDownWindow time.Duration) int32 {
	if history == nil {
		return desired
	}
	history.mu.Lock()
	defer history.mu.Unlock()

	retention := max(scaleUpWindow, scaleDownWindow)
	if retention == 0 {
		delete(history.targets, target)
		return desired
	}
	oldest := now.Add(-retention)
	for candidate, recommendations := range history.targets {
		kept := recommendations[:0]
		for _, recommendation := range recommendations {
			if !recommendation.at.Before(oldest) {
				kept = append(kept, recommendation)
			}
		}
		if len(kept) == 0 {
			delete(history.targets, candidate)
		} else {
			history.targets[candidate] = kept
		}
	}

	history.targets[target] = append(history.targets[target], capacityRecommendation{at: now, groups: desired})
	if desired < current && scaleDownWindow > 0 {
		cutoff := now.Add(-scaleDownWindow)
		for _, recommendation := range history.targets[target] {
			if !recommendation.at.Before(cutoff) && recommendation.groups > desired {
				desired = recommendation.groups
			}
		}
		if desired > current {
			desired = current
		}
	} else if desired > current && scaleUpWindow > 0 {
		cutoff := now.Add(-scaleUpWindow)
		for _, recommendation := range history.targets[target] {
			if !recommendation.at.Before(cutoff) && recommendation.groups < desired {
				desired = recommendation.groups
			}
		}
		if desired < current {
			desired = current
		}
	}
	return desired
}
