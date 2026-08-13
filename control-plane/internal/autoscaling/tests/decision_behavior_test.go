// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"context"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"testing"
)

func TestDecisionsAreRegisteredAndRecommendCapacity(t *testing.T) {
	_, decisions, _ := algorithm.Names()
	for _, name := range []string{"manual", "queue", "threshold"} {
		if !contains(decisions, name) {
			t.Fatalf("decision registry missing %q: %v", name, decisions)
		}
	}
	snapshot := scalingSnapshot()
	manual, err := algorithm.BuildDecision("manual", core.DecisionConfig{})
	if err != nil {
		t.Fatal(err)
	}
	got, _ := manual.CalculateDesiredCapacity(context.Background(), snapshot)
	if got.Groups != snapshot.Capacity.BaselineGroups {
		t.Fatalf("manual=%#v", got)
	}
	queue, err := algorithm.BuildDecision("queue", core.DecisionConfig{TargetQueuePerRoutableGroup: 1})
	if err != nil {
		t.Fatal(err)
	}
	snapshot.Observation.QueueRequests = 2
	snapshot.Observation.ActiveRequests = 1
	got, _ = queue.CalculateDesiredCapacity(context.Background(), snapshot)
	if got.Groups != 3 || got.Reason != core.DesiredCapacityReasonQueuePressure {
		t.Fatalf("queue=%#v", got)
	}
	threshold, err := algorithm.BuildDecision("threshold", core.DecisionConfig{ScaleUpQueue: 1})
	if err != nil {
		t.Fatal(err)
	}
	got, _ = threshold.CalculateDesiredCapacity(context.Background(), snapshot)
	if got.Groups != 3 {
		t.Fatalf("threshold=%#v", got)
	}
	if _, err := algorithm.BuildDecision("", core.DecisionConfig{}); err == nil {
		t.Fatal("empty unknown decision accepted")
	}
}
