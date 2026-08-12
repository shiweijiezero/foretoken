// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package core

import (
	"context"
	"testing"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
)

type fixedAlgorithm struct {
	desired    int32
	target     *algorithm.TargetID
	snapshotID string
}

func (fixedAlgorithm) Name() string { return "fixed" }
func (fixed fixedAlgorithm) Recommend(_ context.Context, snapshot algorithm.Snapshot) (algorithm.Recommendation, error) {
	target := snapshot.Target
	if fixed.target != nil {
		target = *fixed.target
	}
	snapshotID := snapshot.Ref.ID
	if fixed.snapshotID != "" {
		snapshotID = fixed.snapshotID
	}
	return algorithm.Recommendation{Target: target, SnapshotID: snapshotID, Disposition: algorithm.RecommendationApply, DesiredGroups: fixed.desired, Reason: algorithm.ReasonStable, Message: "test"}, nil
}

func TestEvaluatorOwnsBoundsRateLimitsAndTransitionGating(t *testing.T) {
	evaluator := Evaluator{Algorithm: fixedAlgorithm{desired: 10}}
	decision, err := evaluator.Evaluate(context.Background(), testSnapshot(2, 1, 8, false, 1, 0))
	if err != nil {
		t.Fatal(err)
	}
	if decision.AppliedGroups != 3 || decision.Direction != algorithm.DirectionUp || decision.Recommendation.DesiredGroups != 10 {
		t.Fatalf("rate-limited decision = %#v", decision)
	}

	decision, err = evaluator.Evaluate(context.Background(), testSnapshot(2, 1, 8, true, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	if decision.AppliedGroups != 2 || decision.Constraint != algorithm.ReasonTransitionInProgress {
		t.Fatalf("transition decision = %#v", decision)
	}
}

func TestEvaluatorAllowsScaleFromZeroDuringTransition(t *testing.T) {
	decision, err := (Evaluator{Algorithm: fixedAlgorithm{desired: 1}}).Evaluate(context.Background(), testSnapshot(0, 0, 8, true, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	if decision.AppliedGroups != 1 || decision.Direction != algorithm.DirectionUp {
		t.Fatalf("scale-from-zero decision = %#v", decision)
	}
}

func TestEvaluatorRateLimitDoesNotOverflowAtMaxInt32(t *testing.T) {
	const maximum = int32(1<<31 - 1)
	decision, err := (Evaluator{Algorithm: fixedAlgorithm{desired: maximum}}).Evaluate(context.Background(), testSnapshot(maximum, 0, maximum, false, 1, 0))
	if err != nil {
		t.Fatal(err)
	}
	if decision.AppliedGroups != maximum || decision.Direction != algorithm.DirectionHold {
		t.Fatalf("overflowed decision = %#v", decision)
	}
}

func TestEvaluatorClampsScaleDownToMinimum(t *testing.T) {
	decision, err := (Evaluator{Algorithm: fixedAlgorithm{desired: -5}}).Evaluate(context.Background(), testSnapshot(2, 1, 8, false, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	if decision.AppliedGroups != 1 || decision.Direction != algorithm.DirectionDown {
		t.Fatalf("clamped decision = %#v", decision)
	}
}

func TestEvaluatorRejectsMismatchedIdentityAndFence(t *testing.T) {
	snapshot := testSnapshot(1, 0, 8, false, 0, 0)
	other := snapshot.Target
	other.Name = "other"
	if _, err := (Evaluator{Algorithm: fixedAlgorithm{desired: 2, target: &other}}).Evaluate(context.Background(), snapshot); err == nil {
		t.Fatal("mismatched target was accepted")
	}
	if _, err := (Evaluator{Algorithm: fixedAlgorithm{desired: 2, snapshotID: "stale"}}).Evaluate(context.Background(), snapshot); err == nil {
		t.Fatal("stale snapshot fence was accepted")
	}
}

func testSnapshot(current, minimum, maximum int32, transitioning bool, upStep, downStep int32) algorithm.Snapshot {
	return algorithm.Snapshot{
		Target: algorithm.TargetID{ServiceUID: "service-uid", Name: "aggregate", Kind: algorithm.TargetPool, Role: algorithm.RoleAggregate},
		Ref:    algorithm.SnapshotRef{ID: "snapshot-1", PolicyRevision: "policy-1"},
		Capacity: algorithm.CapacityState{
			BaselineGroups:  current,
			RequestedGroups: current,
			Transitioning:   transitioning,
		},
		Limits: algorithm.CapacityLimits{MinGroups: minimum, MaxGroups: maximum, MaxScaleUpStep: upStep, MaxScaleDownStep: downStep},
	}
}
