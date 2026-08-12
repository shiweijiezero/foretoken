// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package algorithm

import "context"

// SimpleStepAlgorithm is the smallest reference implementation for community algorithms.
type SimpleStepAlgorithm struct {
	ScaleUpQueue int64
}

func (SimpleStepAlgorithm) Name() string { return "simple_step" }

func (step SimpleStepAlgorithm) Recommend(_ context.Context, snapshot Snapshot) (Recommendation, error) {
	current := snapshot.Capacity.RequestedGroups
	if snapshot.Observation.State != ObservationFresh || !snapshot.Observation.Window.Complete {
		return simpleStepRecommendation(snapshot, current, RecommendationInsufficientData, ReasonObservationUnavailable, "demand observations are unavailable, stale, or incomplete"), nil
	}
	if snapshot.Observation.QueueRequests > step.ScaleUpQueue {
		if current < snapshot.Limits.MaxGroups {
			return simpleStepRecommendation(snapshot, current+1, RecommendationApply, ReasonQueuePressure, "queue pressure exceeds the configured threshold"), nil
		}
		return simpleStepRecommendation(snapshot, current, RecommendationHold, ReasonAtMaximum, "queue pressure is high but the target is at its maximum"), nil
	}
	if snapshot.Observation.QueueRequests == 0 && snapshot.Observation.ActiveRequests == 0 {
		if current > snapshot.Limits.MinGroups {
			return simpleStepRecommendation(snapshot, current-1, RecommendationApply, ReasonIdle, "the target is idle"), nil
		}
		return simpleStepRecommendation(snapshot, current, RecommendationHold, ReasonAtMinimum, "the target is idle but already at its minimum"), nil
	}
	return simpleStepRecommendation(snapshot, current, RecommendationHold, ReasonStable, "current capacity matches observed demand"), nil
}

func simpleStepRecommendation(snapshot Snapshot, desired int32, disposition RecommendationDisposition, reason RecommendationReason, message string) Recommendation {
	return Recommendation{Target: snapshot.Target, SnapshotID: snapshot.Ref.ID, Disposition: disposition, DesiredGroups: desired, Reason: reason, Message: message}
}
