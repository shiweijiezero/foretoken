// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Runs the existing Helm and GPU-free controller lifecycle against an explicit disposable cluster.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apiMeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

const (
	platformNamespace = "foretoken-platform"
	modelNamespace    = "foretoken-demo"
	lwsNamespace      = "lws-system"
	cacheDirectory    = "/var/lib/foretoken-runtime-cache"
)

var retainedCRDs = []string{
	"crd/inferencedeployments.inference.foretoken.io",
	"crd/runtimecaches.inference.foretoken.io",
	"crd/frontendservices.inference.foretoken.io",
	"crd/modelservices.inference.foretoken.io",
	"crd/modelpools.inference.foretoken.io",
	"crd/modelgroups.inference.foretoken.io",
}

// deploymentValidation owns only namespaces it creates and releases installed within them.
// CRDs remain installed, matching the platform's uninstall contract.
type deploymentValidation struct {
	root              string
	context           string
	namespaces        []string
	instanceSubmitted bool
}

// main provides Makefile callers with chart-only checks or the existing controller scenario.
func main() {
	root := flag.String("root", "", "Repository root containing the chart and Quick Start")
	kubeContext := flag.String("context", "", "Explicit kubeconfig context for a disposable single-node cluster")
	chartOnly := flag.Bool("chart-only", false, "Validate rendered Helm resources without accessing Kubernetes")
	flag.Parse()
	if *root == "" || (!*chartOnly && *kubeContext == "") {
		fmt.Fprintln(os.Stderr, "--root and, for deployment validation, an explicit --context are required")
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	validation := deploymentValidation{root: *root, context: *kubeContext}
	err := validation.verifyChart(ctx)
	if err == nil && !*chartOnly {
		err = validation.verifyController(ctx)
		if err == nil {
			err = validation.verifyUninstalled(ctx)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// verifyController exercises installation, instance reconciliation, update and deletion.
// Cleanup retains its own deadline after failure or cancellation of the scenario.
func (v *deploymentValidation) verifyController(ctx context.Context) (err error) {
	defer func() {
		if err != nil {
			diagnosticsCtx, cancel := context.WithTimeout(context.Background(), time.Minute)
			v.diagnostics(diagnosticsCtx)
			cancel()
		}
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
		defer cancel()
		err = errors.Join(err, v.cleanup(cleanupCtx))
	}()
	if err = v.install(ctx); err != nil {
		return err
	}
	return v.reconcileQuickStart(ctx)
}

// install uses the real LWS and Foretoken charts in namespaces created by this invocation.
func (v *deploymentValidation) install(ctx context.Context) error {
	for _, namespace := range []string{lwsNamespace, platformNamespace, modelNamespace} {
		if _, err := v.kubectl(ctx, nil, "create", "namespace", namespace); err != nil {
			return err
		}
		v.namespaces = append(v.namespaces, namespace)
	}
	version, err := v.command(ctx, nil, "go", "-C", "control-plane", "list", "-m", "-f", "{{.Version}}", "sigs.k8s.io/lws")
	if err != nil {
		return err
	}
	if _, err := v.helm(ctx, "upgrade", "--install", "foretoken-lws", "oci://registry.k8s.io/lws/charts/lws",
		"--namespace", lwsNamespace, "--version", strings.TrimPrefix(strings.TrimSpace(string(version)), "v"),
		"--wait", "--timeout=3m"); err != nil {
		return err
	}
	var nodes corev1.NodeList
	if err := v.get(ctx, &nodes, "nodes"); err != nil {
		return err
	}
	if len(nodes.Items) != 1 {
		return fmt.Errorf("controller validation requires a disposable single-node cluster; found %d nodes", len(nodes.Items))
	}
	if _, err := v.helm(ctx, "upgrade", "--install", "foretoken", "deploy/charts/foretoken",
		"--namespace", platformNamespace,
		"--set", "image.repository=foretoken-control-plane", "--set", "image.tag=test", "--set", "image.pullPolicy=Never",
		"--set", "workload.cache.directories.foretoken-runtime-cache-test.directory="+cacheDirectory,
		"--set", "workload.cache.directories.foretoken-runtime-cache-test.hostPath="+cacheDirectory,
		"--set", "workload.cache.directories.foretoken-runtime-cache-test.capacity=100Gi",
		"--set", "workload.cache.directories.foretoken-runtime-cache-test.hostnames[0]="+nodes.Items[0].Name,
		"--wait", "--timeout=3m"); err != nil {
		return err
	}
	_, err = v.kubectl(ctx, nil, append([]string{"wait", "--for=condition=Established"}, append(retainedCRDs, "--timeout=1m")...)...)
	return err
}

// reconcileQuickStart submits actual example resources through InferenceDeployment's API.
// It observes controller materialization, not model readiness or inference execution.
func (v *deploymentValidation) reconcileQuickStart(ctx context.Context) error {
	rendered, err := v.kubectl(ctx, nil, "kustomize", "examples/quickstart/qwen3-0.6b")
	if err != nil {
		return err
	}
	resources, err := decodeResources(rendered)
	if err != nil {
		return err
	}
	deployment := inferencev1alpha1.InferenceDeployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "InferenceDeployment"},
		ObjectMeta: metav1.ObjectMeta{Name: "lifecycle", Namespace: modelNamespace},
		Spec:       inferencev1alpha1.InferenceDeploymentSpec{UpdateID: "initial"},
	}
	modelName := ""
	for _, resource := range resources {
		deployment.Spec.Resources = append(deployment.Spec.Resources, inferencev1alpha1.DeploymentResourceReference{
			APIVersion: resource.GetAPIVersion(), Kind: resource.GetKind(), Name: resource.GetName(),
		})
		if resource.GetKind() == "ModelService" {
			modelName = resource.GetName()
		}
		if resource.GetKind() == "RuntimeCache" {
			if err := unstructured.SetNestedField(resource.Object, cacheDirectory, "spec", "directory"); err != nil {
				return err
			}
		}
	}
	if modelName == "" {
		return errors.New("Quick Start has no ModelService")
	}
	v.instanceSubmitted = true
	if _, err := v.kubectl(ctx, &deployment, "apply", "-f", "-"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "wait", "--for=condition=Ready", "inferencedeployment/lifecycle", "-n", modelNamespace, "--timeout=3m"); err != nil {
		return err
	}
	if err := v.get(ctx, &deployment, "inferencedeployment/lifecycle", "-n", modelNamespace); err != nil {
		return err
	}
	// Membership comes from the submitted instance, after the platform prepares its directories.
	for _, resource := range resources {
		labels := resource.GetLabels()
		if labels == nil {
			labels = make(map[string]string)
		}
		labels[inferencev1alpha1.DeploymentUIDLabel] = string(deployment.UID)
		resource.SetLabels(labels)
		resource.SetNamespace(modelNamespace)
	}
	if _, err := v.kubectl(ctx, map[string]any{"apiVersion": "v1", "kind": "List", "items": resources},
		"apply", "--server-side", "-n", modelNamespace, "-f", "-"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "wait", "--for=condition=Ready", "runtimecache/models", "-n", modelNamespace, "--timeout=1m"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "wait", "--for=condition=PoolsMaterialized", "modelservice/"+modelName, "-n", modelNamespace, "--timeout=1m"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "get", "modelpool/"+modelName+"-default", "-n", modelNamespace); err != nil {
		return err
	}
	var model inferencev1alpha1.ModelService
	if err := v.get(ctx, &model, "modelservice/"+modelName, "-n", modelNamespace); err != nil {
		return err
	}
	if ready := apiMeta.FindStatusCondition(model.Status.Conditions, "Ready"); ready == nil || ready.Status != metav1.ConditionFalse {
		return fmt.Errorf("GPU-free ModelService unexpectedly reports readiness: %v", ready)
	}
	if _, err := v.helm(ctx, "upgrade", "foretoken", "deploy/charts/foretoken", "--namespace", platformNamespace,
		"--reuse-values", "--set", "resources.requests.cpu=21m", "--wait", "--timeout=3m"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "patch", "inferencedeployment/lifecycle", "-n", modelNamespace,
		"--type=merge", "-p", `{"spec":{"updateID":"updated"}}`); err != nil {
		return err
	}
	if err := v.get(ctx, &deployment, "inferencedeployment/lifecycle", "-n", modelNamespace); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "wait", fmt.Sprintf("--for=jsonpath={.status.observedGeneration}=%d", deployment.Generation),
		"inferencedeployment/lifecycle", "-n", modelNamespace, "--timeout=3m"); err != nil {
		return err
	}
	if _, err := v.kubectl(ctx, nil, "wait", "--for=condition=Ready", "inferencedeployment/lifecycle", "-n", modelNamespace, "--timeout=3m"); err != nil {
		return err
	}
	var controller appsv1.Deployment
	controllerName := "deployment/foretoken-" + string(deployment.UID)
	if err := v.get(ctx, &controller, controllerName, "-n", modelNamespace); err != nil {
		return err
	}
	updated := false
	for _, container := range controller.Spec.Template.Spec.Containers {
		if container.Name == "manager" {
			updated = container.Resources.Requests.Cpu().String() == "21m"
		}
	}
	if !updated {
		return errors.New("instance controller did not select the updated 21m CPU request")
	}
	if _, err := v.kubectl(ctx, nil, "delete", "inferencedeployment/lifecycle", "-n", modelNamespace,
		"--cascade=background", "--wait", "--timeout=2m"); err != nil {
		return err
	}
	_, err = v.kubectl(ctx, nil, "wait", "--for=delete", controllerName, "-n", modelNamespace, "--timeout=2m")
	return err
}

// cleanup retires the instance before removing its namespace, retained directory PVs and charts.
func (v *deploymentValidation) cleanup(ctx context.Context) error {
	var failures []error
	if slices.Contains(v.namespaces, modelNamespace) {
		if v.instanceSubmitted {
			_, err := v.kubectl(ctx, nil, "delete", "inferencedeployment/lifecycle", "-n", modelNamespace,
				"--ignore-not-found", "--cascade=background", "--wait", "--timeout=2m")
			failures = append(failures, err)
		}
		var claims corev1.PersistentVolumeClaimList
		failures = append(failures, v.get(ctx, &claims, "pvc", "-n", modelNamespace))
		_, err := v.kubectl(ctx, nil, "delete", "namespace", modelNamespace, "--wait", "--timeout=1m")
		failures = append(failures, err)
		for _, claim := range claims.Items {
			if claim.Spec.VolumeName != "" {
				_, err := v.kubectl(ctx, nil, "delete", "pv", claim.Spec.VolumeName, "--ignore-not-found", "--wait", "--timeout=1m")
				failures = append(failures, err)
			}
		}
	}
	for _, release := range []struct{ name, namespace string }{{"foretoken", platformNamespace}, {"foretoken-lws", lwsNamespace}} {
		if slices.Contains(v.namespaces, release.namespace) {
			_, err := v.helm(ctx, "uninstall", release.name, "--namespace", release.namespace, "--ignore-not-found", "--wait", "--timeout=1m")
			failures = append(failures, err)
			_, err = v.kubectl(ctx, nil, "delete", "namespace", release.namespace, "--wait", "--timeout=1m")
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

// verifyUninstalled checks the existing retention boundary after the owned releases are gone.
func (v *deploymentValidation) verifyUninstalled(ctx context.Context) error {
	if _, err := v.kubectl(ctx, nil, append([]string{"get"}, retainedCRDs...)...); err != nil {
		return err
	}
	remaining, err := v.kubectl(ctx, nil, "get", "clusterrole/foretoken-platform-foretoken-deployment-manager", "--ignore-not-found", "-o", "name")
	if err == nil && len(bytes.TrimSpace(remaining)) != 0 {
		err = errors.New("platform deployment-manager ClusterRole remains after uninstall")
	}
	return err
}

// diagnostics preserves cluster, CRD hook and controller failures before cleanup removes their Pods.
func (v *deploymentValidation) diagnostics(ctx context.Context) {
	for _, args := range [][]string{
		{"get", "pods", "--all-namespaces", "-o", "wide"},
		{"get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"},
		{"logs", "-n", platformNamespace, "job/foretoken-crd-apply", "--all-containers", "--tail=200"},
		{"logs", "-n", platformNamespace, "deployment/foretoken-deployment-manager", "--all-containers", "--tail=200"},
		{"logs", "-n", platformNamespace, "deployment/foretoken-deployment-manager", "--all-containers", "--previous", "--tail=200"},
	} {
		output, err := v.kubectl(ctx, nil, args...)
		fmt.Print(string(output))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
		}
	}
}

// get decodes actual Kubernetes JSON without relying on presentation formatting.
func (v *deploymentValidation) get(ctx context.Context, result any, args ...string) error {
	output, err := v.kubectl(ctx, nil, append([]string{"get"}, append(args, "-o", "json")...)...)
	if err != nil {
		return err
	}
	return json.Unmarshal(output, result)
}

// kubectl keeps every cluster operation on the explicitly selected test context.
func (v *deploymentValidation) kubectl(ctx context.Context, input any, args ...string) ([]byte, error) {
	return v.command(ctx, input, "kubectl", append([]string{"--context", v.context}, args...)...)
}

// helm binds install, update and uninstall to the same test context as kubectl.
func (v *deploymentValidation) helm(ctx context.Context, args ...string) ([]byte, error) {
	return v.command(ctx, nil, "helm", append([]string{"--kube-context", v.context}, args...)...)
}

// command runs real tools from the repository root and preserves their stderr and failure output.
func (v *deploymentValidation) command(ctx context.Context, input any, name string, args ...string) ([]byte, error) {
	command := exec.CommandContext(ctx, name, args...)
	command.Dir = filepath.Clean(v.root)
	command.Stderr = os.Stderr
	if input != nil {
		value, err := json.Marshal(input)
		if err != nil {
			return nil, err
		}
		command.Stdin = bytes.NewReader(value)
	}
	fmt.Fprintln(os.Stderr, "$", name, strings.Join(args, " "))
	output, err := command.Output()
	if err != nil {
		return output, fmt.Errorf("%s %s: %w\n%s", name, strings.Join(args, " "), err, output)
	}
	return output, nil
}
