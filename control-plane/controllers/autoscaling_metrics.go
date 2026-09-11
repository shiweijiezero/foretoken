// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"strings"

	"github.com/prometheus/client_golang/prometheus"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// autoscalingCollector projects published status from the manager cache at scrape time. It keeps
// no decision history: deletion, disabled autoscaling, and leader changes follow Kubernetes state.
// Only successfully published capacity is called applied; planning alone does not update metrics.
type autoscalingCollector struct {
	reader client.Reader

	recommendation *prometheus.Desc
	adjusted       *prometheus.Desc
	applied        *prometheus.Desc
	ready          *prometheus.Desc
	routable       *prometheus.Desc
	evaluated      *prometheus.Desc
	observed       *prometheus.Desc
	fresh          *prometheus.Desc
	stage          *prometheus.Desc
	direction      *prometheus.Desc
	condition      *prometheus.Desc
}

// newAutoscalingCollector binds the controller's cached ModelService reader to a Prometheus collector.
func newAutoscalingCollector(reader client.Reader) *autoscalingCollector {
	labels := []string{"namespace", "modelservice", "target_kind", "target_name", "role"}
	desc := func(name, help string) *prometheus.Desc {
		return prometheus.NewDesc("foretoken_autoscaling_"+name, help, labels, nil)
	}
	return &autoscalingCollector{
		reader:         reader,
		recommendation: desc("recommendation_replicas", "Available algorithm recommendation in the latest published evaluation."),
		adjusted:       desc("adjusted_replicas", "Capacity after stabilization and rate limiting in the latest published evaluation."),
		applied:        desc("applied_replicas", "Capacity successfully applied to every ModelPool represented by the published target."),
		ready:          desc("ready_replicas", "Ready replicas observed by the latest published evaluation."),
		routable:       desc("routable_replicas", "Routable replicas observed by the latest published evaluation."),
		evaluated:      desc("evaluation_timestamp_seconds", "Unix time of the latest published autoscaling evaluation."),
		observed:       desc("observation_timestamp_seconds", "Unix time of the oldest source observation used in the published evaluation; absent without observations."),
		fresh:          desc("observation_fresh", "Whether the latest published autoscaling observation was Fresh according to the controller."),
		stage: prometheus.NewDesc("foretoken_autoscaling_stage", "Latest published pipeline stage result; messages are omitted from metric labels.",
			append(append([]string(nil), labels...), "stage", "algorithm", "disposition", "reason"), nil),
		direction: prometheus.NewDesc("foretoken_autoscaling_direction", "Latest published capacity direction after lifecycle constraints.",
			append(append([]string(nil), labels...), "direction"), nil),
		condition: prometheus.NewDesc("foretoken_model_service_condition", "Current-generation ModelService condition, including compilation and capacity application failures.",
			[]string{"namespace", "modelservice", "condition", "status", "reason"}, nil),
	}
}

// Describe enumerates the fixed metric families registered by the ModelService controller.
func (collector *autoscalingCollector) Describe(ch chan<- *prometheus.Desc) {
	for _, desc := range []*prometheus.Desc{
		collector.recommendation, collector.adjusted, collector.applied, collector.ready,
		collector.routable, collector.evaluated, collector.observed, collector.fresh,
		collector.stage, collector.direction, collector.condition,
	} {
		ch <- desc
	}
}

// Collect reads one cached snapshot, publishing current service conditions and the latest complete
// autoscaling evaluations. Timestamps let consumers detect stalled reconciliation without cached ages.
func (collector *autoscalingCollector) Collect(ch chan<- prometheus.Metric) {
	var services inferencev1alpha1.ModelServiceList
	if err := collector.reader.List(context.Background(), &services); err != nil {
		ch <- prometheus.NewInvalidMetric(collector.evaluated, err)
		return
	}
	for index := range services.Items {
		service := &services.Items[index]
		if !service.DeletionTimestamp.IsZero() {
			continue
		}
		for _, condition := range service.Status.Conditions {
			if condition.ObservedGeneration == service.Generation {
				ch <- prometheus.MustNewConstMetric(collector.condition, prometheus.GaugeValue, 1,
					service.Namespace, service.Name, condition.Type, string(condition.Status), condition.Reason)
			}
		}
		if service.Spec.Autoscaling == nil || service.Status.ObservedGeneration != service.Generation {
			continue
		}
		for _, target := range service.Status.Autoscaling {
			labels := []string{service.Namespace, service.Name, target.Kind, strings.TrimPrefix(target.ID, target.Kind+"/"), target.Role}
			emit := func(desc *prometheus.Desc, value float64) {
				ch <- prometheus.MustNewConstMetric(desc, prometheus.GaugeValue, value, labels...)
			}
			if target.Decision.Disposition == string(core.RecommendationAvailable) {
				emit(collector.recommendation, float64(target.Decision.DesiredReplicas))
			}
			emit(collector.adjusted, float64(target.Adjustment.AdjustedReplicas))
			emit(collector.applied, float64(target.AppliedReplicas))
			emit(collector.ready, float64(target.ReadyReplicas))
			emit(collector.routable, float64(target.RoutableReplicas))
			emit(collector.evaluated, float64(target.EvaluatedAt.Unix()))
			if target.ObservationEndAt != nil {
				emit(collector.observed, float64(target.ObservationEndAt.Unix()))
			}
			if target.ObservationState == string(core.MetricsFresh) {
				emit(collector.fresh, 1)
			} else {
				emit(collector.fresh, 0)
			}
			stages := []struct {
				name   string
				status *inferencev1alpha1.AutoscalingStageStatus
			}{
				{"trigger", target.Trigger},
				{"decision", &target.Decision.AutoscalingStageStatus},
				{"adjustment", &target.Adjustment.AutoscalingStageStatus},
			}
			for _, stage := range stages {
				if stage.status != nil {
					ch <- prometheus.MustNewConstMetric(collector.stage, prometheus.GaugeValue, 1,
						append(append([]string(nil), labels...), stage.name, stage.status.Algorithm, stage.status.Disposition, stage.status.Reason)...)
				}
			}
			if target.Constraint != nil {
				ch <- prometheus.MustNewConstMetric(collector.stage, prometheus.GaugeValue, 1,
					append(append([]string(nil), labels...), "constraint", "", "Apply", target.Constraint.Reason)...)
			}
			ch <- prometheus.MustNewConstMetric(collector.direction, prometheus.GaugeValue, 1,
				append(append([]string(nil), labels...), target.Direction)...)
		}
	}
}

var _ prometheus.Collector = (*autoscalingCollector)(nil)
