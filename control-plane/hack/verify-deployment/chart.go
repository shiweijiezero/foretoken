// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Checks the chart's existing default, explicit monitoring and API-discovered monitoring renders.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"slices"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/yaml"
)

// verifyChart checks semantic resource fields in the three existing Helm configurations.
func (v *deploymentValidation) verifyChart(ctx context.Context) error {
	if output, err := v.command(ctx, nil, "helm", "lint", "deploy/charts/foretoken", "--kube-version", "1.36.3"); err != nil {
		return err
	} else {
		fmt.Print(string(output))
	}
	base := []string{"template", "foretoken", "deploy/charts/foretoken", "--kube-version", "1.36.3"}
	rendered, err := v.command(ctx, nil, "helm", base...)
	if err != nil {
		return err
	}
	resources, err := decodeResources(rendered)
	if err != nil {
		return err
	}
	kinds := make(map[string]bool)
	for _, resource := range resources {
		kinds[resource.GetKind()] = true
	}
	if !kinds["Job"] || !kinds["ClusterRole"] || kinds["ServiceMonitor"] || kinds["PrometheusRule"] || kinds["CustomResourceDefinition"] {
		return fmt.Errorf("default chart has unexpected resource kinds: %v", kinds)
	}

	for _, scenario := range []struct {
		name     string
		explicit bool
		args     []string
	}{
		{"explicit monitoring", true, []string{
			"--set", "frontend.enabled=true", "--set", "observability.mode=enabled",
			"--set-string", "observability.additionalLabels.release=kube-prometheus-stack",
			"--set", "observability.interval=30s", "--set", "observability.scrapeTimeout=10s",
		}},
		{"API-discovered monitoring", false, []string{
			"--api-versions", "monitoring.coreos.com/v1/ServiceMonitor",
			"--api-versions", "monitoring.coreos.com/v1/PrometheusRule", "--set", "frontend.enabled=true",
		}},
	} {
		rendered, err := v.command(ctx, nil, "helm", append(slices.Clone(base), scenario.args...)...)
		if err != nil {
			return err
		}
		resources, err := decodeResources(rendered)
		if err != nil {
			return err
		}
		if err := verifyMonitoring(resources, scenario.explicit); err != nil {
			return fmt.Errorf("%s: %w", scenario.name, err)
		}
	}
	return nil
}

// verifyMonitoring binds each monitor to its endpoint and checks recording-rule identity.
func verifyMonitoring(resources []*unstructured.Unstructured, explicit bool) error {
	ports := map[string]bool{"metrics": true, "http": true, "model-server": true}
	monitors, ruleResources := 0, 0
	for _, resource := range resources {
		switch resource.GetKind() {
		case "ServiceMonitor":
			var monitor monitoringv1.ServiceMonitor
			if err := runtime.DefaultUnstructuredConverter.FromUnstructured(resource.Object, &monitor); err != nil {
				return err
			}
			if monitor.Namespace != "default" || !monitor.Spec.NamespaceSelector.Any || len(monitor.Spec.Endpoints) != 1 {
				return fmt.Errorf("unexpected ServiceMonitor scope: %s/%s", monitor.Namespace, monitor.Name)
			}
			endpoint := monitor.Spec.Endpoints[0]
			if !ports[endpoint.Port] || endpoint.Path != "/metrics" {
				return fmt.Errorf("ServiceMonitor %s has unexpected metrics endpoint: %+v", monitor.Name, endpoint)
			}
			if explicit && (endpoint.Interval != "30s" || endpoint.ScrapeTimeout != "10s" || monitor.Labels["release"] != "kube-prometheus-stack") {
				return fmt.Errorf("ServiceMonitor %s did not apply explicit monitoring values", monitor.Name)
			}
			if endpoint.Port == "metrics" {
				if monitor.Spec.Selector.MatchLabels["app.kubernetes.io/name"] != "foretoken-control-plane" || monitor.Spec.Selector.MatchLabels["app.kubernetes.io/instance"] != "foretoken" {
					return errors.New("control-plane monitor does not select Foretoken controller services")
				}
			} else {
				key := "inference.foretoken.io/frontend-service"
				if endpoint.Port == "model-server" {
					key = "inference.foretoken.io/model-group"
				}
				expressions := monitor.Spec.Selector.MatchExpressions
				if len(expressions) != 1 || expressions[0].Key != key || expressions[0].Operator != metav1.LabelSelectorOpExists {
					return fmt.Errorf("ServiceMonitor %s does not select its workload services", monitor.Name)
				}
			}
			delete(ports, endpoint.Port)
			monitors++
		case "PrometheusRule":
			var rules monitoringv1.PrometheusRule
			if err := runtime.DefaultUnstructuredConverter.FromUnstructured(resource.Object, &rules); err != nil {
				return err
			}
			if rules.Namespace != "default" || (explicit && rules.Labels["release"] != "kube-prometheus-stack") {
				return fmt.Errorf("unexpected recording-rule identity or labels: %s/%s", rules.Namespace, rules.Name)
			}
			records := make(map[string]bool)
			for _, group := range rules.Spec.Groups {
				if group.Name == "foretoken.alerting" {
					return errors.New("recording rules unexpectedly contain the foretoken.alerting group")
				}
				for _, rule := range group.Rules {
					records[rule.Record] = true
				}
			}
			if !records["foretoken:frontend_http_response_starts:rate5m"] || !records["foretoken:model_server_requests_waiting:sum"] {
				return errors.New("PrometheusRule is missing the frontend or model-server recording rule")
			}
			ruleResources++
		}
	}
	if monitors != 3 || ruleResources != 1 {
		return fmt.Errorf("expected three metrics monitors and one recording-rule resource; got %d and %d", monitors, ruleResources)
	}
	return nil
}

// decodeResources uses Kubernetes' YAML stream decoder for Helm and Kustomize output.
func decodeResources(contents []byte) ([]*unstructured.Unstructured, error) {
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(contents), 4096)
	var resources []*unstructured.Unstructured
	for {
		resource := new(unstructured.Unstructured)
		if err := decoder.Decode(&resource.Object); err == io.EOF {
			return resources, nil
		} else if err != nil {
			return nil, err
		}
		if len(resource.Object) != 0 {
			resources = append(resources, resource)
		}
	}
}
