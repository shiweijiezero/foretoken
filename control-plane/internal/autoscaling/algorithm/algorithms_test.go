// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package algorithm

import (
	"context"
	"math"
	"testing"
)

func TestQueueAlgorithmThresholdComparisonDoesNotOverflow(t *testing.T) {
	snapshot := testSnapshot(2, 2, 1, 8)
	decision, err := (QueueAlgorithm{TargetQueuePerRoutableGroup: math.MaxInt64}).Recommend(context.Background(), snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if decision.DesiredGroups != 1 || decision.Disposition != RecommendationApply || decision.Reason != ReasonIdle {
		t.Fatalf("overflowed queue decision = %#v", decision)
	}
}

func TestQueueAlgorithmSaturatesAtCapacityBounds(t *testing.T) {
	const maximum = int32(math.MaxInt32)
	upSnapshot := testSnapshot(maximum, maximum, 0, maximum)
	upSnapshot.Observation.QueueRequests = 1
	up, err := (QueueAlgorithm{}).Recommend(context.Background(), upSnapshot)
	if err != nil {
		t.Fatal(err)
	}
	if up.DesiredGroups != maximum || up.Disposition != RecommendationHold || up.Reason != ReasonAtMaximum {
		t.Fatalf("maximum decision = %#v", up)
	}

	down, err := (QueueAlgorithm{}).Recommend(context.Background(), testSnapshot(0, 1, 0, maximum))
	if err != nil {
		t.Fatal(err)
	}
	if down.DesiredGroups != 0 || down.Disposition != RecommendationHold || down.Reason != ReasonAtMinimum {
		t.Fatalf("minimum decision = %#v", down)
	}
}

func TestQueueAlgorithmDistinguishesUnavailableStaleAndObservedZero(t *testing.T) {
	for state, reason := range map[ObservationState]RecommendationReason{
		ObservationUnavailable: ReasonObservationUnavailable,
		ObservationStale:       ReasonObservationStale,
	} {
		snapshot := testSnapshot(2, 2, 0, 8)
		snapshot.Observation.State = state
		decision, err := (QueueAlgorithm{}).Recommend(context.Background(), snapshot)
		if err != nil {
			t.Fatal(err)
		}
		if decision.Disposition != RecommendationInsufficientData || decision.DesiredGroups != 2 || decision.Reason != reason {
			t.Fatalf("state %q decision = %#v", state, decision)
		}
	}
}

func TestSimpleStepAlgorithmIsSideEffectFreeAndExplainsItsDecision(t *testing.T) {
	snapshot := testSnapshot(2, 0, 0, 8)
	snapshot.Observation.QueueRequests = 5
	decision, err := (SimpleStepAlgorithm{ScaleUpQueue: 4}).Recommend(context.Background(), snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if decision.DesiredGroups != 3 || decision.Disposition != RecommendationApply || decision.Reason != ReasonQueuePressure {
		t.Fatalf("recommendation = %#v", decision)
	}
	if snapshot.Capacity.RequestedGroups != 2 {
		t.Fatalf("algorithm mutated input = %#v", snapshot)
	}
}

func testSnapshot(current, routable, minimum, maximum int32) Snapshot {
	return Snapshot{
		Target:      TargetID{ServiceUID: "service-uid", Name: "aggregate", Kind: TargetPool, Role: RoleAggregate},
		Ref:         SnapshotRef{ID: "snapshot-1"},
		Capacity:    CapacityState{RequestedGroups: current, RoutableGroups: routable},
		Limits:      CapacityLimits{MinGroups: minimum, MaxGroups: maximum},
		Observation: DemandObservation{State: ObservationFresh, Window: ObservationWindow{Complete: true}},
	}
}
