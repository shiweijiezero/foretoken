// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles service-owned alert rules without changing serving readiness.

package observability

import (
	"context"
	"fmt"
	"maps"
	"reflect"
	"strings"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const conditionAlertsReady = "AlertsReady"

// ServiceAlerts binds service-owned rules to the Prometheus selected at platform installation.
type ServiceAlerts struct {
	client.Client
	APIReader  client.Reader
	Prometheus client.ObjectKey
	Labels     map[string]string
	WatchRules bool
}

// NewServiceAlerts creates the shared alert resource reconciler used by service controllers.
// An empty Prometheus reference permits normal serving but cannot enable alert rules.
func NewServiceAlerts(manager ctrl.Manager, reference string, ruleLabels map[string]string) (*ServiceAlerts, error) {
	result := &ServiceAlerts{Client: manager.GetClient(), APIReader: manager.GetAPIReader(), Labels: maps.Clone(ruleLabels)}
	// An optional monitoring API must not prevent the serving controllers' caches from starting.
	_, err := manager.GetRESTMapper().RESTMapping(monitoringv1.SchemeGroupVersion.WithKind("PrometheusRule").GroupKind(), monitoringv1.SchemeGroupVersion.Version)
	if err != nil && !meta.IsNoMatchError(err) {
		return nil, err
	}
	result.WatchRules = err == nil
	if reference != "" {
		namespace, name, found := strings.Cut(reference, "/")
		if !found || namespace == "" || name == "" || strings.Contains(name, "/") {
			return nil, fmt.Errorf("observability Prometheus must use NAMESPACE/NAME")
		}
		result.Prometheus = client.ObjectKey{Namespace: namespace, Name: name}
	}
	return result, nil
}

// Reconcile creates, updates, or removes the service-owned PrometheusRule after checking its monitoring selection.
func (alerts *ServiceAlerts) Reconcile(ctx context.Context, owner client.Object, selected []string, scope Scope, thresholds *inferencev1alpha1.ModelAlertThresholds, admission *inferencev1alpha1.AdmissionAlertThresholds) error {
	active := len(selected) != 0 && owner.GetDeletionTimestamp().IsZero()
	if alerts == nil {
		if active {
			return fmt.Errorf("service alerts require shared monitoring configured by foretoken install")
		}
		return nil
	}
	key := client.ObjectKey{Namespace: owner.GetNamespace(), Name: "foretoken-alerts-" + string(owner.GetUID())}
	current := new(monitoringv1.PrometheusRule)
	reader := client.Reader(alerts.Client)
	if !alerts.WatchRules {
		reader = alerts.APIReader
	}
	err := reader.Get(ctx, key, current)
	if !active {
		if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read service alert rules for removal: %w", err)
		}
		if !metav1.IsControlledBy(current, owner) {
			return nil
		}
		return client.IgnoreNotFound(alerts.Delete(ctx, current))
	}
	if err != nil && !apierrors.IsNotFound(err) && !meta.IsNoMatchError(err) {
		return fmt.Errorf("read service alert rules: %w", err)
	}
	if err == nil && !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("PrometheusRule %q is not owned by this service", key.Name)
	}
	if err := alerts.requireSelection(ctx, owner.GetNamespace()); err != nil {
		return err
	}
	spec, err := Render(selected, scope, thresholds, admission)
	if err != nil {
		return err
	}
	for groupIndex := range spec.Groups {
		for ruleIndex := range spec.Groups[groupIndex].Rules {
			nameLabel := "model_service"
			if scope.Frontend != "" {
				nameLabel = "frontend_service"
			}
			spec.Groups[groupIndex].Rules[ruleIndex].Labels[nameLabel] = owner.GetName()
		}
	}
	desired := &monitoringv1.PrometheusRule{
		TypeMeta:   metav1.TypeMeta{APIVersion: monitoringv1.SchemeGroupVersion.String(), Kind: "PrometheusRule"},
		ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace, Labels: maps.Clone(alerts.Labels)},
		Spec:       spec,
	}
	if err := controllerutil.SetControllerReference(owner, desired, alerts.Scheme()); err != nil {
		return err
	}
	if err := alerts.Patch(ctx, desired, client.Apply, client.FieldOwner("foretoken-service-alerts"), client.ForceOwnership); err != nil {
		return fmt.Errorf("apply service alert rules: %w", err)
	}
	return nil
}

// requireSelection checks the actual Prometheus selectors rather than claiming an unused rule is ready.
func (alerts *ServiceAlerts) requireSelection(ctx context.Context, namespace string) error {
	if alerts.Prometheus.Name == "" {
		return fmt.Errorf("service alerts require shared monitoring configured by foretoken install")
	}
	prometheus := new(monitoringv1.Prometheus)
	if err := alerts.APIReader.Get(ctx, alerts.Prometheus, prometheus); err != nil {
		return fmt.Errorf("read selected Prometheus: %w", err)
	}
	selector, err := metav1.LabelSelectorAsSelector(prometheus.Spec.RuleSelector)
	if err != nil {
		return err
	}
	if !selector.Matches(labels.Set(alerts.Labels)) {
		return fmt.Errorf("Prometheus %s does not select service alert labels", alerts.Prometheus)
	}
	if prometheus.Spec.RuleNamespaceSelector == nil {
		if namespace == prometheus.Namespace {
			return nil
		}
	} else {
		selector, err := metav1.LabelSelectorAsSelector(prometheus.Spec.RuleNamespaceSelector)
		if err != nil {
			return err
		}
		value := new(corev1.Namespace)
		if err := alerts.APIReader.Get(ctx, client.ObjectKey{Name: namespace}, value); err != nil {
			return err
		}
		if selector.Matches(labels.Set(value.Labels)) {
			return nil
		}
	}
	return fmt.Errorf("Prometheus %s does not select alert rules in namespace %s", alerts.Prometheus, namespace)
}

// UpdateServiceCondition reports optional monitoring configuration separately from serving conditions.
func UpdateServiceCondition(ctx context.Context, c client.Client, owner client.Object, active bool, alertErr error) error {
	if !owner.GetDeletionTimestamp().IsZero() {
		return nil
	}
	var conditions *[]metav1.Condition
	switch service := owner.(type) {
	case *inferencev1alpha1.ModelService:
		conditions = &service.Status.Conditions
	case *inferencev1alpha1.FrontendService:
		conditions = &service.Status.Conditions
	}
	if !active && alertErr == nil && meta.FindStatusCondition(*conditions, conditionAlertsReady) == nil {
		return nil
	}
	before := owner.DeepCopyObject().(client.Object)
	if !active && alertErr == nil {
		meta.RemoveStatusCondition(conditions, conditionAlertsReady)
	} else {
		condition := metav1.Condition{Type: conditionAlertsReady, Status: metav1.ConditionTrue, Reason: "Configured", Message: "Selected service alert rules are configured", ObservedGeneration: owner.GetGeneration()}
		if alertErr != nil {
			condition.Status, condition.Reason, condition.Message = metav1.ConditionFalse, "ConfigurationFailed", alertErr.Error()
		}
		meta.SetStatusCondition(conditions, condition)
	}
	if reflect.DeepEqual(before, owner) {
		return nil
	}
	return c.Status().Patch(ctx, owner, client.MergeFrom(before))
}
