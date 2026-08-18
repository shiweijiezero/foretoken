// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"testing"
)

func TestTriggersAreRegisteredAndRespectObservations(t *testing.T) {
	snapshot := scalingSnapshot()
	periodic, err := algorithm.BuildTrigger("periodic", core.TriggerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	if got := periodic.Decide(snapshot); got.Disposition != core.TriggerFire {
		t.Fatalf("periodic=%#v", got)
	}
	snapshot.Observation.State = core.ObservationStale
	if got := periodic.Decide(snapshot); got.Disposition != core.TriggerInsufficientData {
		t.Fatalf("stale=%#v", got)
	}
	watermark, err := algorithm.BuildTrigger("watermark", core.TriggerConfig{LowQueuePerRoutableGroup: 0, HighQueuePerRoutableGroup: 1})
	if err != nil {
		t.Fatal(err)
	}
	snapshot.Observation.State = core.ObservationFresh
	snapshot.Observation.QueueRequests = 2
	snapshot.Observation.ActiveRequests = 1
	if got := watermark.Decide(snapshot); got.Disposition != core.TriggerFire || got.Reason != core.TriggerReasonHighWatermark {
		t.Fatalf("watermark=%#v", got)
	}
	if _, err := autoscaling.New(autoscaling.Configuration{DecisionAlgorithm: autoscaling.DecisionAlgorithmQueue, TriggerAlgorithm: "unknown"}); err == nil {
		t.Fatal("unknown trigger accepted")
	}
}
