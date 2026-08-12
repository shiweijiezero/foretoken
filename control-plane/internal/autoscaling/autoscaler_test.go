// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package autoscaling

import (
	"context"
	"testing"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
)

func TestBuiltinsHaveStableNamesAndDistinctBehavior(t *testing.T) {
	manual := Manual()
	queue, err := New(Configuration{Algorithm: AlgorithmQueue, TargetQueuePerRoutableGroup: 2})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := algorithm.Snapshot{
		Target:      algorithm.TargetID{ServiceUID: "service-uid", Name: "aggregate", Kind: algorithm.TargetPool, Role: algorithm.RoleAggregate},
		Ref:         algorithm.SnapshotRef{ID: "snapshot-1"},
		Capacity:    algorithm.CapacityState{BaselineGroups: 2, RequestedGroups: 2, RoutableGroups: 2},
		Limits:      algorithm.CapacityLimits{MinGroups: 0, MaxGroups: 8},
		Observation: algorithm.DemandObservation{State: algorithm.ObservationFresh, Window: algorithm.ObservationWindow{Complete: true}, QueueRequests: 5},
	}
	manualDecisions, err := manual.Plan(context.Background(), []algorithm.Snapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	queueDecisions, err := queue.Plan(context.Background(), []algorithm.Snapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if manualDecisions[0].Algorithm != "manual" || manualDecisions[0].AppliedGroups != 2 {
		t.Fatalf("manual decision = %#v", manualDecisions)
	}
	if queueDecisions[0].Algorithm != "queue" || queueDecisions[0].AppliedGroups != 3 {
		t.Fatalf("queue decision = %#v", queueDecisions)
	}
}

func TestPlannerRejectsDuplicateTargetsAndUnknownAlgorithms(t *testing.T) {
	if _, err := New(Configuration{Algorithm: "unknown"}); err == nil {
		t.Fatal("unknown algorithm was accepted")
	}
	snapshot := algorithm.Snapshot{
		Target:   algorithm.TargetID{ServiceUID: "service-uid", Name: "aggregate", Kind: algorithm.TargetPool, Role: algorithm.RoleAggregate},
		Ref:      algorithm.SnapshotRef{ID: "snapshot-1"},
		Capacity: algorithm.CapacityState{BaselineGroups: 1, RequestedGroups: 1},
		Limits:   algorithm.CapacityLimits{MaxGroups: 8},
	}
	_, err := Manual().Plan(context.Background(), []algorithm.Snapshot{snapshot, snapshot})
	if err == nil {
		t.Fatal("duplicate target snapshots were accepted")
	}
}
