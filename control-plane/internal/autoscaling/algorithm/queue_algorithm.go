// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package algorithm

import "context"

// QueueAlgorithm scales complete Groups from queue pressure and an idle signal.
type QueueAlgorithm struct {
	TargetQueuePerRoutableGroup int64
}

func (QueueAlgorithm) Name() string { return "queue" }

func (queue QueueAlgorithm) Recommend(_ context.Context, snapshot Snapshot) (Recommendation, error) {
	current := snapshot.Capacity.RequestedGroups
	switch snapshot.Observation.State {
	case ObservationUnavailable:
		return queueRecommendation(snapshot, RecommendationInsufficientData, current, ReasonObservationUnavailable, "demand observations are unavailable"), nil
	case ObservationStale:
		return queueRecommendation(snapshot, RecommendationInsufficientData, current, ReasonObservationStale, "demand observations are stale"), nil
	case ObservationFresh:
		if !snapshot.Observation.Window.Complete {
			return queueRecommendation(snapshot, RecommendationInsufficientData, current, ReasonObservationIncomplete, "demand observation window is incomplete"), nil
		}
	default:
		return queueRecommendation(snapshot, RecommendationInsufficientData, current, ReasonObservationUnavailable, "demand observations are unavailable"), nil
	}

	supply := snapshot.Capacity.RoutableGroups
	if supply < 1 {
		supply = 1
	}
	target := queue.TargetQueuePerRoutableGroup
	if target < 0 {
		target = 0
	}
	if exceedsQueueTarget(snapshot.Observation.QueueRequests, target, supply) {
		if current < snapshot.Limits.MaxGroups {
			return queueRecommendation(snapshot, RecommendationApply, current+1, ReasonQueuePressure, "queue pressure exceeds routable capacity"), nil
		}
		return queueRecommendation(snapshot, RecommendationHold, current, ReasonAtMaximum, "queue pressure is high but the target is at its maximum"), nil
	}
	if snapshot.Observation.QueueRequests == 0 && snapshot.Observation.ActiveRequests == 0 {
		if current > snapshot.Limits.MinGroups {
			return queueRecommendation(snapshot, RecommendationApply, current-1, ReasonIdle, "the target is idle"), nil
		}
		return queueRecommendation(snapshot, RecommendationHold, current, ReasonAtMinimum, "the target is idle but already at its minimum"), nil
	}
	return queueRecommendation(snapshot, RecommendationHold, current, ReasonStable, "current capacity matches observed demand"), nil
}

func exceedsQueueTarget(requests, target int64, supply int32) bool {
	if requests <= 0 {
		return false
	}
	if target <= 0 {
		return true
	}
	groups := int64(supply)
	quotient, remainder := requests/groups, requests%groups
	return quotient > target || quotient == target && remainder > 0
}

func queueRecommendation(snapshot Snapshot, disposition RecommendationDisposition, desired int32, reason RecommendationReason, message string) Recommendation {
	return Recommendation{
		Target:        snapshot.Target,
		SnapshotID:    snapshot.Ref.ID,
		Disposition:   disposition,
		DesiredGroups: desired,
		Reason:        reason,
		Message:       message,
	}
}
