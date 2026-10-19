// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package deployments

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"reflect"
	"slices"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/uuid"
	"sigs.k8s.io/controller-runtime/pkg/client"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

// legacyController identifies an older namespace-wide manager that would also reconcile instance resources.
func (r *Reconciler) legacyController(ctx context.Context, namespace string) (string, error) {
	var controllers appsv1.DeploymentList
	if err := r.APIReader.List(ctx, &controllers, client.InNamespace(namespace), client.MatchingLabels{"app.kubernetes.io/name": "foretoken-control-plane"}); err != nil {
		return "", err
	}
	for _, controller := range controllers.Items {
		for _, container := range controller.Spec.Template.Spec.Containers {
			if container.Name != "manager" || controllerArgument(&container, "deployment-uid") != "" {
				continue
			}
			if len(container.Args) > 0 && slices.Contains([]string{"manage-deployments", "maintain-model-cache", "maintain-log-storage"}, container.Args[0]) {
				continue
			}
			return controller.Name, nil
		}
	}
	return "", nil
}

// controllerArgument reads the chart's compiled flag, keeping defaults at the chart boundary.
func controllerArgument(controller *corev1.Container, name string) string {
	for _, argument := range controller.Args {
		if value, found := strings.CutPrefix(argument, "--"+name+"="); found {
			return value
		}
	}
	return ""
}

func setControllerArgument(controller *corev1.Container, name, value string) {
	prefix := "--" + name + "="
	for index, argument := range controller.Args {
		if strings.HasPrefix(argument, prefix) {
			controller.Args[index] = prefix + value
			return
		}
	}
	controller.Args = append(controller.Args, prefix+value)
}

func managerContainer(controller *appsv1.Deployment) (*corev1.Container, error) {
	for index := range controller.Spec.Template.Spec.Containers {
		if controller.Spec.Template.Spec.Containers[index].Name == "manager" {
			return &controller.Spec.Template.Spec.Containers[index], nil
		}
	}
	return nil, fmt.Errorf("platform controller template has no manager container")
}

// configureController projects instance identity and an optional completed source publication once.
func (r *Reconciler) configureController(ctx context.Context, deployment *api.InferenceDeployment, selection, previous *controllerSelection) error {
	controller := &selection.Template.Controller
	r.bind(deployment, controller)
	container, err := managerContainer(controller)
	if err != nil {
		return err
	}
	pod := &controller.Spec.Template
	// Telemetry budgets are read at process startup. Roll only when their selected
	// configuration changes, not whenever the CLI sends a fresh update request.
	const configurationRevision = "inference.foretoken.io/configuration-revision"
	pod.Annotations = maps.Clone(pod.Annotations)
	if pod.Annotations == nil {
		pod.Annotations = make(map[string]string)
	}
	if previous != nil && reflect.DeepEqual(previous.Template.Configuration.Data, selection.Template.Configuration.Data) {
		pod.Annotations[configurationRevision] = previous.Template.Controller.Spec.Template.Annotations[configurationRevision]
	} else {
		pod.Annotations[configurationRevision] = string(uuid.NewUUID())
	}
	pod.Labels = maps.Clone(pod.Labels)
	if pod.Labels == nil {
		pod.Labels = make(map[string]string)
	}
	pod.Labels[api.DeploymentUIDLabel] = string(deployment.UID)
	pod.Labels["app.kubernetes.io/component"] = controllerComponent
	controller.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{
		api.DeploymentUIDLabel:        string(deployment.UID),
		"app.kubernetes.io/component": controllerComponent,
	}}
	pod.Spec.ServiceAccountName = controller.Name
	for index := range pod.Spec.Volumes {
		if pod.Spec.Volumes[index].Name == "controller-configuration" && pod.Spec.Volumes[index].ConfigMap != nil {
			pod.Spec.Volumes[index].ConfigMap.Name = controller.Name
		}
	}
	setControllerArgument(container, "deployment-uid", string(deployment.UID))

	// Platform profile names stay unchanged. Only references to declared native
	// resources follow the same name prefix as the submitted Kustomize root.
	for index, argument := range container.Args {
		name, value, found := strings.Cut(strings.TrimPrefix(argument, "--"), "=")
		if !found || value == "" {
			continue
		}
		kind := ""
		switch name {
		case "vllm-ec-shared-storage-claim":
			kind = "PersistentVolumeClaim"
		case "vllm-mooncake-store-config-map-name":
			kind = "ConfigMap"
		case "model-source-token-secret-name", "admission-store-url-secret-name", "response-store-url-secret-name", "workload-image-pull-secret":
			kind = "Secret"
		}
		if kind != "" {
			for _, resource := range deployment.Spec.Resources {
				if resource.APIVersion == "v1" && resource.Kind == kind && resource.Name == deployment.Name+"-"+value {
					container.Args[index] = "--" + name + "=" + resource.Name
					break
				}
			}
		}
	}
	if revision := selection.ControllerSourceRevision; revision != "" {
		if controllerArgument(container, "source-mode") != "true" {
			return fmt.Errorf("controller source updates require a source-installed platform")
		}
		if err := runtimeconfig.ValidateSourceRevision(revision); err != nil {
			return err
		}
		var files runtimeconfig.ApplicationFiles
		if err := json.Unmarshal([]byte(controllerArgument(container, "application-files")), &files); err != nil {
			return fmt.Errorf("decode controller application tools: %w", err)
		}
		image, err := files.ResolveImage(ctx, "control-plane", revision, container.Image)
		if err != nil {
			return err
		}
		pod.Spec.InitContainers = slices.DeleteFunc(pod.Spec.InitContainers, func(value corev1.Container) bool { return value.Name == "application-files" })
		pod.Spec.Volumes = slices.DeleteFunc(pod.Spec.Volumes, func(value corev1.Volume) bool { return value.Name == "application" })
		container.VolumeMounts = slices.DeleteFunc(container.VolumeMounts, func(value corev1.VolumeMount) bool { return value.Name == "application" })
		container.Image = image
		reference := files.Ref("control-plane", revision)
		files.Configure(pod, container, reference, "manager")
		setControllerArgument(container, "video-worker-image", image)
		setControllerArgument(container, "video-worker-application-url", reference)
	}
	return nil
}

// materialize prepares dependencies before the controller and returns its actual API identity.
func (r *Reconciler) materialize(ctx context.Context, deployment *api.InferenceDeployment, selection *controllerSelection, configuration *corev1.ConfigMap) (*appsv1.Deployment, error) {
	bindings, err := r.prepareDirectories(ctx, deployment.Namespace, selection)
	if err != nil {
		return nil, err
	}
	controller := selection.Template.Controller.DeepCopy()
	container, err := managerContainer(controller)
	if err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(bindings)
	if err != nil {
		return nil, err
	}
	setControllerArgument(container, "cache-directory-bindings", string(encoded))
	gateway, err := r.gatewayAccess(ctx, controllerArgument(container, "frontend-mode"), controllerArgument(container, "frontend-gateway-name"), controllerArgument(container, "frontend-gateway-namespace"), controllerArgument(container, "frontend-gateway-section-name"))
	if err != nil {
		return nil, err
	}
	access, err := json.Marshal(struct {
		CacheClaim        string            `json:"cacheClaim"`
		DirectoryBindings map[string]string `json:"directoryBindings"`
		Gateway           map[string]any    `json:"gateway"`
	}{controllerArgument(container, "cache-claim"), bindings, gateway})
	if err != nil {
		return nil, err
	}
	configuration.Data[AccessKey] = string(access)
	if err := r.apply(ctx, deployment, configuration); err != nil {
		return nil, fmt.Errorf("publish deployment access: %w", err)
	}

	// Registry credentials are copied by the platform owner, never read by member CLIs.
	for index, reference := range controller.Spec.Template.Spec.ImagePullSecrets {
		original := new(corev1.Secret)
		if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: r.Configuration.Namespace, Name: reference.Name}, original); err != nil {
			return nil, fmt.Errorf("read controller registry credential: %w", err)
		}
		credential := &corev1.Secret{
			TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
			Type:     original.Type, Data: original.Data,
		}
		r.bind(deployment, credential)
		credential.Name += fmt.Sprintf("-pull-%d", index)
		if err := r.apply(ctx, deployment, credential); err != nil {
			return nil, err
		}
		controller.Spec.Template.Spec.ImagePullSecrets[index].Name = credential.Name
	}
	if err := r.preparePermissions(ctx, deployment, selection.Template.Rules, container); err != nil {
		return nil, err
	}
	if service := selection.Template.MetricsService; service != nil {
		service := service.DeepCopy()
		r.bind(deployment, service)
		service.Spec.Selector = maps.Clone(controller.Spec.Selector.MatchLabels)
		if err := r.apply(ctx, deployment, service); err != nil {
			return nil, err
		}
	}
	if err := r.apply(ctx, deployment, controller); err != nil {
		return nil, fmt.Errorf("apply instance controller: %w", err)
	}
	return controller, nil
}

// preparePermissions delegates serving permissions to one instance while retaining privileged setup here.
func (r *Reconciler) preparePermissions(ctx context.Context, deployment *api.InferenceDeployment, rules []rbacv1.PolicyRule, container *corev1.Container) error {
	account := &corev1.ServiceAccount{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"}}
	r.bind(deployment, account)
	role := &rbacv1.Role{TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"}, Rules: slices.Clone(rules)}
	r.bind(deployment, role)
	// These credentials are administrator-selected namespace references, not discovery rights.
	secretNames := []string{}
	for _, flag := range []string{"model-source-token-secret-name", "admission-store-url-secret-name", "response-store-url-secret-name"} {
		if name := controllerArgument(container, flag); name != "" {
			secretNames = append(secretNames, name)
		}
	}
	if len(secretNames) > 0 {
		role.Rules = append(role.Rules, rbacv1.PolicyRule{APIGroups: []string{""}, Resources: []string{"secrets"}, ResourceNames: secretNames, Verbs: []string{"get"}})
	}
	subjects := []rbacv1.Subject{{Kind: "ServiceAccount", Name: account.Name, Namespace: account.Namespace}}
	binding := &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"},
		RoleRef:  rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: role.Name}, Subjects: subjects,
	}
	r.bind(deployment, binding)
	clusterRole := &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "ClusterRole"},
		Rules: []rbacv1.PolicyRule{
			{APIGroups: []string{""}, Resources: []string{"nodes"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{""}, Resources: []string{"namespaces"}, ResourceNames: []string{deployment.Namespace}, Verbs: []string{"get"}},
		},
	}
	r.bind(deployment, clusterRole)
	clusterRole.Namespace = ""
	clusterBinding := &rbacv1.ClusterRoleBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "ClusterRoleBinding"},
		RoleRef:  rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "ClusterRole", Name: clusterRole.Name}, Subjects: subjects,
	}
	r.bind(deployment, clusterBinding)
	clusterBinding.Namespace = ""
	for _, object := range []client.Object{account, role, binding, clusterRole, clusterBinding} {
		if err := r.apply(ctx, deployment, object); err != nil {
			return err
		}
	}
	if reference := controllerArgument(container, "observability-prometheus"); reference != "" {
		namespace, name, found := strings.Cut(reference, "/")
		if !found || namespace == "" || name == "" {
			return fmt.Errorf("observability Prometheus requires NAMESPACE/NAME")
		}
		role := &rbacv1.Role{
			TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"},
			Rules:    []rbacv1.PolicyRule{{APIGroups: []string{"monitoring.coreos.com"}, Resources: []string{"prometheuses"}, ResourceNames: []string{name}, Verbs: []string{"get"}}},
		}
		r.bind(deployment, role)
		role.Name += "-prometheus"
		role.Namespace = namespace
		binding := &rbacv1.RoleBinding{
			TypeMeta: metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"},
			RoleRef:  rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: role.Name}, Subjects: subjects,
		}
		r.bind(deployment, binding)
		binding.Name, binding.Namespace = role.Name, namespace
		for _, object := range []client.Object{role, binding} {
			if err := r.apply(ctx, deployment, object); err != nil {
				return err
			}
		}
	}
	return nil
}

// gatewayAccess publishes only the public listener so ordinary endpoint discovery stays namespace-local.
func (r *Reconciler) gatewayAccess(ctx context.Context, mode, name, namespace, section string) (map[string]any, error) {
	access := make(map[string]any)
	if mode != "gateway" {
		return access, nil
	}
	gateway := new(gatewayv1.Gateway)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, gateway); err != nil {
		return nil, fmt.Errorf("read frontend Gateway: %w", err)
	}
	if len(gateway.Status.Addresses) == 0 {
		return nil, fmt.Errorf("waiting for Gateway %s/%s to publish its address", namespace, name)
	}
	var selected *gatewayv1.Listener
	for index := range gateway.Spec.Listeners {
		listener := &gateway.Spec.Listeners[index]
		if section == "" || string(listener.Name) == section {
			if selected != nil {
				return nil, fmt.Errorf("Gateway %s/%s requires an explicit listener selection", namespace, name)
			}
			selected = listener
		}
	}
	if selected == nil {
		return nil, fmt.Errorf("Gateway %s/%s has no selected listener %q", namespace, name, section)
	}
	scheme := "http"
	if selected.Protocol == gatewayv1.HTTPSProtocolType || selected.Protocol == gatewayv1.TLSProtocolType {
		scheme = "https"
	}
	return map[string]any{"name": name, "namespace": namespace, "sectionName": section, "address": gateway.Status.Addresses[0].Value, "scheme": scheme, "port": selected.Port}, nil
}
