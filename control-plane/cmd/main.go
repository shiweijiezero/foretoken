// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Starts the Foretoken control-plane manager and health endpoints.

package main

import (
	"errors"
	"flag"
	"os"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
)

const (
	frontendModeLocal   = "local"
	frontendModeGateway = "gateway"
)

func main() {
	var metricsAddress string
	var probeAddress string
	var leaderElection bool
	var frontendEnabled bool
	var frontendMode string
	var frontendImage string
	var frontendPort int
	var frontendGatewayName string
	var frontendGatewayNamespace string
	var frontendGatewaySectionName string
	var inferenceEngineProfileRevision string
	var inferenceEngineImage string
	var modelServerPort int
	var gpuResourceName string
	var runtimeClassName string
	var gpuNodeSelectorKey string
	var gpuNodeSelectorValue string
	var vllmECProfileName string
	var vllmECProfileRevision string
	var vllmECConnector string
	var vllmECSharedStorageClaim string
	var vllmECSharedStoragePath string
	var vllmPDProfileName string
	var vllmPDProfileRevision string
	var vllmPDProtocol string
	var vllmPDBootstrapPort int
	var vllmPDAbortRequestTimeoutSeconds int
	var vllmPDRDMADeviceName string
	var vllmPDRDMAResourceName string
	var vllmPDRDMAResourceCount int
	var vllmMooncakeStoreProfileName string
	var vllmMooncakeStoreProfileRevision string
	var vllmMooncakeStoreConfigMapName string
	var vllmMooncakeStoreConfigMapKey string
	var vllmMooncakeStorePythonHashSeed string
	var autoscalingTelemetryCollectionTimeout time.Duration
	var autoscalingTelemetryRequestTimeout time.Duration
	var autoscalingTelemetryConcurrency int
	var workloadImagePullSecretNames []string

	// Metrics stay disabled until the chart exposes a secured endpoint.
	flag.StringVar(&metricsAddress, "metrics-bind-address", "0", "Metrics endpoint bind address; 0 disables metrics.")
	flag.StringVar(&probeAddress, "health-probe-bind-address", ":8081", "Health probe bind address.")
	flag.BoolVar(&leaderElection, "leader-elect", false, "Enable leader election.")
	flag.DurationVar(&autoscalingTelemetryCollectionTimeout, "autoscaling-telemetry-collection-timeout", 3*time.Second, "Total budget for one autoscaling telemetry observation.")
	flag.DurationVar(&autoscalingTelemetryRequestTimeout, "autoscaling-telemetry-request-timeout", time.Second, "Timeout for one autoscaling telemetry HTTP request.")
	flag.IntVar(&autoscalingTelemetryConcurrency, "autoscaling-telemetry-concurrency", 8, "Maximum concurrent autoscaling telemetry HTTP requests per source type.")
	flag.BoolVar(&frontendEnabled, "frontend-enabled", false, "Enable FrontendService workload reconciliation.")
	flag.StringVar(&frontendMode, "frontend-mode", frontendModeLocal, "Frontend access mode: local or gateway.")
	flag.StringVar(&frontendImage, "frontend-image", "", "Frontend runtime image.")
	flag.IntVar(&frontendPort, "frontend-port", 8080, "Frontend runtime HTTP port.")
	flag.StringVar(&frontendGatewayName, "frontend-gateway-name", "", "Platform Gateway name used by frontend HTTPRoutes.")
	flag.StringVar(&frontendGatewayNamespace, "frontend-gateway-namespace", "", "Platform Gateway namespace; defaults to the FrontendService namespace.")
	flag.StringVar(&frontendGatewaySectionName, "frontend-gateway-section-name", "", "Platform Gateway listener section name.")
	flag.Func("workload-image-pull-secret", "Namespace-local image pull Secret used by frontend and model-server Pods; repeat for multiple Secrets.", func(value string) error {
		if value == "" {
			return errors.New("workload image pull Secret name must be nonempty")
		}
		workloadImagePullSecretNames = append(workloadImagePullSecretNames, value)
		return nil
	})
	flag.StringVar(&inferenceEngineProfileRevision, "inference-engine-profile-revision", "default", "Opaque revision of the configured inference engine profile.")
	flag.StringVar(&inferenceEngineImage, "inference-engine-image", "", "Inference engine image containing the Foretoken model-server adapter.")
	flag.IntVar(&modelServerPort, "model-server-port", 9000, "Internal model-server HTTP port.")
	flag.StringVar(&gpuResourceName, "gpu-resource-name", "nvidia.com/gpu", "Kubernetes extended resource used for accelerator devices.")
	flag.StringVar(&runtimeClassName, "runtime-class-name", "", "Optional RuntimeClass for inference engine Pods.")
	flag.StringVar(&gpuNodeSelectorKey, "gpu-node-selector-key", "", "Optional GPU node label key.")
	flag.StringVar(&gpuNodeSelectorValue, "gpu-node-selector-value", "", "Optional GPU node label value.")
	flag.StringVar(&vllmECProfileName, "vllm-ec-profile-name", "", "Platform-owned EC profile name; empty disables E/P/D.")
	flag.StringVar(&vllmECProfileRevision, "vllm-ec-profile-revision", "", "Platform-owned EC profile revision.")
	flag.StringVar(&vllmECConnector, "vllm-ec-connector", "", "Fixed EC connector identity for the configured vLLM adapter.")
	flag.StringVar(&vllmECSharedStorageClaim, "vllm-ec-shared-storage-claim", "", "ReadWriteMany PVC used by the configured EC connector.")
	flag.StringVar(&vllmECSharedStoragePath, "vllm-ec-shared-storage-path", "/var/lib/foretoken/ec", "In-container shared EC storage path.")
	flag.StringVar(&vllmPDProfileName, "vllm-pd-profile-name", "", "Opaque platform-owned Mooncake P/D profile name; empty disables P/D.")
	flag.StringVar(&vllmPDProfileRevision, "vllm-pd-profile-revision", "", "Opaque platform-owned Mooncake P/D profile revision.")
	flag.StringVar(&vllmPDProtocol, "vllm-pd-protocol", "", "Mooncake P/D protocol; only rdma is supported.")
	flag.IntVar(&vllmPDBootstrapPort, "vllm-pd-bootstrap-port", 0, "Mooncake bootstrap port.")
	flag.IntVar(&vllmPDAbortRequestTimeoutSeconds, "vllm-pd-abort-request-timeout-seconds", 0, "Mooncake abort request timeout in seconds.")
	flag.StringVar(&vllmPDRDMADeviceName, "vllm-pd-rdma-device-name", "", "Platform-verified Mooncake RDMA HCA name.")
	flag.StringVar(&vllmPDRDMAResourceName, "vllm-pd-rdma-resource-name", "", "Kubernetes extended resource that injects Mooncake RDMA devices.")
	flag.IntVar(&vllmPDRDMAResourceCount, "vllm-pd-rdma-resource-count", 0, "Mooncake RDMA extended resources requested by each P/D Pod.")
	flag.StringVar(&vllmMooncakeStoreProfileName, "vllm-mooncake-store-profile-name", "", "Opaque platform-owned Mooncake Store profile name; empty disables external Store.")
	flag.StringVar(&vllmMooncakeStoreProfileRevision, "vllm-mooncake-store-profile-revision", "", "Opaque platform-owned Mooncake Store profile revision.")
	flag.StringVar(&vllmMooncakeStoreConfigMapName, "vllm-mooncake-store-config-map-name", "", "Mooncake Store ConfigMap name.")
	flag.StringVar(&vllmMooncakeStoreConfigMapKey, "vllm-mooncake-store-config-map-key", "", "Mooncake Store ConfigMap key.")
	flag.StringVar(&vllmMooncakeStorePythonHashSeed, "vllm-mooncake-store-python-hash-seed", "", "PYTHONHASHSEED for Mooncake Store.")

	logOptions := zap.Options{Development: false}
	logOptions.BindFlags(flag.CommandLine)
	flag.Parse()
	workloadImagePullSecrets := make([]corev1.LocalObjectReference, len(workloadImagePullSecretNames))
	for index, name := range workloadImagePullSecretNames {
		workloadImagePullSecrets[index] = corev1.LocalObjectReference{Name: name}
	}
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&logOptions)))
	if modelServerPort < 1 || modelServerPort > 65535 {
		ctrl.Log.Error(errors.New("model-server-port must be between 1 and 65535"), "invalid inference engine profile")
		os.Exit(1)
	}
	if autoscalingTelemetryCollectionTimeout <= 0 || autoscalingTelemetryRequestTimeout <= 0 || autoscalingTelemetryConcurrency < 1 {
		ctrl.Log.Error(errors.New("autoscaling telemetry timeouts and concurrency must be positive"), "invalid autoscaling telemetry settings")
		os.Exit(1)
	}
	if frontendMode != frontendModeLocal && frontendMode != frontendModeGateway {
		ctrl.Log.Error(errors.New("frontend-mode must be local or gateway"), "invalid frontend profile")
		os.Exit(1)
	}
	if vllmPDProfileName != "" && (vllmPDBootstrapPort < 1 || vllmPDBootstrapPort > 65535 || vllmPDAbortRequestTimeoutSeconds < 1 || int64(vllmPDAbortRequestTimeoutSeconds) > int64(1<<31-1) || vllmPDRDMAResourceCount < 1 || int64(vllmPDRDMAResourceCount) > int64(1<<31-1)) {
		ctrl.Log.Error(errors.New("vLLM P/D numeric settings are outside their supported ranges"), "invalid P/D profile")
		os.Exit(1)
	}
	var mooncakePD *resolver.MooncakePDProfile
	var ec *resolver.ECProfile
	var mooncakeStore *resolver.MooncakeStoreProfile
	if vllmECProfileName != "" {
		ec = &resolver.ECProfile{
			Name: vllmECProfileName, Revision: vllmECProfileRevision,
			Connector:          vllmECConnector,
			SharedStorageClaim: vllmECSharedStorageClaim,
			SharedStoragePath:  vllmECSharedStoragePath,
		}
	}
	if vllmMooncakeStoreProfileName != "" {
		mooncakeStore = &resolver.MooncakeStoreProfile{Name: vllmMooncakeStoreProfileName, Revision: vllmMooncakeStoreProfileRevision, ConfigMapName: vllmMooncakeStoreConfigMapName, ConfigMapKey: vllmMooncakeStoreConfigMapKey, PythonHashSeed: vllmMooncakeStorePythonHashSeed}
	}
	if vllmPDProfileName != "" {
		mooncakePD = &resolver.MooncakePDProfile{
			Name:                       vllmPDProfileName,
			Revision:                   vllmPDProfileRevision,
			Protocol:                   vllmPDProtocol,
			BootstrapPort:              int32(vllmPDBootstrapPort),
			AbortRequestTimeoutSeconds: int32(vllmPDAbortRequestTimeoutSeconds),
			RDMADeviceName:             vllmPDRDMADeviceName,
			RDMAResourceName:           vllmPDRDMAResourceName,
			RDMAResourceCount:          int32(vllmPDRDMAResourceCount),
		}
	}

	// Register built-in and Foretoken APIs before controllers are attached to the manager.
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(inferencev1alpha1.AddToScheme(scheme))
	utilruntime.Must(gatewayv1.Install(scheme))

	restConfig := ctrl.GetConfigOrDie()
	if frontendEnabled && frontendMode == frontendModeGateway {
		discoveryClient, err := discovery.NewDiscoveryClientForConfig(restConfig)
		if err != nil {
			ctrl.Log.Error(err, "unable to create Gateway API discovery client")
			os.Exit(1)
		}
		resources, err := discoveryClient.ServerResourcesForGroupVersion(gatewayv1.GroupVersion.String())
		if err != nil {
			ctrl.Log.Error(err, "frontend requires the Gateway API HTTPRoute CRD")
			os.Exit(1)
		}
		httpRouteAvailable := false
		for _, resource := range resources.APIResources {
			if resource.Name == "httproutes" {
				httpRouteAvailable = true
				break
			}
		}
		if !httpRouteAvailable {
			ctrl.Log.Error(errors.New("HTTPRoute resource was not discovered"), "frontend requires the Gateway API HTTPRoute CRD")
			os.Exit(1)
		}
	}

	manager, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme: scheme,
		Client: client.Options{Cache: &client.CacheOptions{
			// The shared credential is read by its fixed name; avoid a cluster-wide Secret informer.
			DisableFor: []client.Object{&corev1.Secret{}},
		}},
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		LeaderElection:         leaderElection,
		LeaderElectionID:       "inference.foretoken.io",
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}

	// Controllers are registered explicitly so each resource keeps one lifecycle owner.
	if frontendEnabled {
		var gateway *controllers.GatewayParent
		if frontendMode == frontendModeGateway {
			gateway = &controllers.GatewayParent{
				Name:        frontendGatewayName,
				Namespace:   frontendGatewayNamespace,
				SectionName: frontendGatewaySectionName,
			}
		}
		frontendReconciler := &controllers.FrontendServiceReconciler{
			Client:    manager.GetClient(),
			APIReader: manager.GetAPIReader(),
			RuntimeProfile: controllers.FrontendRuntimeProfile{
				Image:            frontendImage,
				Port:             int32(frontendPort),
				ImagePullSecrets: workloadImagePullSecrets,
				Gateway:          gateway,
			},
		}
		if err := frontendReconciler.SetupWithManager(manager); err != nil {
			ctrl.Log.Error(err, "unable to register FrontendService controller")
			os.Exit(1)
		}
	}
	if err := (&controllers.ModelServiceReconciler{
		Client: manager.GetClient(),
		MetricsProvider: controllers.NewHTTPScalingMetricsProvider(manager.GetClient(), controllers.AutoscalingTelemetryOptions{
			CollectionTimeout: autoscalingTelemetryCollectionTimeout,
			RequestTimeout:    autoscalingTelemetryRequestTimeout,
			Concurrency:       autoscalingTelemetryConcurrency,
		}),
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelService controller")
		os.Exit(1)
	}
	if err := (&controllers.KVServiceReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVService controller")
		os.Exit(1)
	}
	if err := (&controllers.KVPoolReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVPool controller")
		os.Exit(1)
	}
	if err := (&controllers.KVGroupReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVGroup controller")
		os.Exit(1)
	}
	if err := (&controllers.ModelPoolReconciler{
		Client: manager.GetClient(),
		TemplateResolver: resolver.StaticModelPoolResolver{RuntimeProfile: resolver.RuntimeProfile{
			Revision:           inferenceEngineProfileRevision,
			Image:              inferenceEngineImage,
			ModelServerPort:    int32(modelServerPort),
			DeviceResourceName: gpuResourceName,
			RuntimeClassName:   runtimeClassName,
			NodeSelectorKey:    gpuNodeSelectorKey,
			NodeSelectorValue:  gpuNodeSelectorValue,
			MooncakePD:         mooncakePD,
			EC:                 ec,
			MooncakeStore:      mooncakeStore,
		}},
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelPool controller")
		os.Exit(1)
	}
	controlPlaneNamespace := os.Getenv("POD_NAMESPACE")
	if controlPlaneNamespace == "" {
		ctrl.Log.Error(errors.New("POD_NAMESPACE is required"), "unable to configure ModelGroup drain networking")
		os.Exit(1)
	}
	if err := (&controllers.ModelGroupReconciler{Client: manager.GetClient(), ControlPlaneNamespace: controlPlaneNamespace, ImagePullSecrets: workloadImagePullSecrets}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelGroup controller")
		os.Exit(1)
	}

	// These endpoints are consumed by the liveness and readiness probes in the Helm chart.
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to register health check")
		os.Exit(1)
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to register readiness check")
		os.Exit(1)
	}

	ctrl.Log.Info("starting manager")
	if err := manager.Start(ctrl.SetupSignalHandler()); err != nil {
		ctrl.Log.Error(err, "manager stopped with an error")
		os.Exit(1)
	}
}
