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
	Target              TargetID
	EvaluatedAt         time.Time
	CurrentReplicas     int32
	RecommendedReplicas int32
	Limits              ReplicaLimits
}

// ReplicaAdjustment is the Adjustment stage output after stabilization and rate limiting.
type ReplicaAdjustment struct {
	Replicas int32
	Reason   AdjustmentReason
	Message  string
}

type AdjustmentConfig struct {
	ScaleUpStabilizationWindow   time.Duration
	ScaleDownStabilizationWindow time.Duration
	History                      *RecommendationHistory
}

type AdjustmentAlgorithm interface {
	Name() string
	Adjust(AdjustmentInput) (ReplicaAdjustment, error)
}

type replicaRecommendation struct {
	at       time.Time
	replicas int32
}

// RecommendationHistory retains recent replica recommendations used by stabilization windows.
type RecommendationHistory struct {
	mu      sync.Mutex
	targets map[TargetID][]replicaRecommendation
}

// NewRecommendationHistory creates empty in-memory recommendation history for one controller process.
func NewRecommendationHistory() *RecommendationHistory {
	return &RecommendationHistory{targets: make(map[TargetID][]replicaRecommendation)}
}

// Stabilize records one replica recommendation and returns the conservative value for its direction.
func (history *RecommendationHistory) Stabilize(target TargetID, now time.Time, current, recommended int32, scaleUpWindow, scaleDownWindow time.Duration) int32 {
	if history == nil {
		return recommended
	}
	history.mu.Lock()
	defer history.mu.Unlock()

	retention := max(scaleUpWindow, scaleDownWindow)
	if retention == 0 {
		delete(history.targets, target)
		return recommended
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

	history.targets[target] = append(history.targets[target], replicaRecommendation{at: now, replicas: recommended})
	if recommended < current && scaleDownWindow > 0 {
		cutoff := now.Add(-scaleDownWindow)
		for _, recommendation := range history.targets[target] {
			if !recommendation.at.Before(cutoff) && recommendation.replicas > recommended {
				recommended = recommendation.replicas
			}
		}
		if recommended > current {
			recommended = current
		}
	} else if recommended > current && scaleUpWindow > 0 {
		cutoff := now.Add(-scaleUpWindow)
		for _, recommendation := range history.targets[target] {
			if !recommendation.at.Before(cutoff) && recommendation.replicas < recommended {
				recommended = recommendation.replicas
			}
		}
		if recommended < current {
			recommended = current
		}
	}
	return recommended
}
