// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Starts the Foretoken controller manager and registers domain controllers.

package main

import (
	"encoding/json"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/frontend"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/kv"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/model"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/profiling"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/storage"
	"github.com/shiweijiezero/foretoken/control-plane/controllers/video"
	"github.com/shiweijiezero/foretoken/control-plane/internal/observability"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"

	"github.com/shiweijiezero/foretoken/control-plane/internal/deployments"
)

const (
	frontendModeLocal   = "local"
	frontendModeGateway = "gateway"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "maintain-log-storage" {
		if err := maintainLogStorage(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
			log.Fatal(err)
		}
		return
	}

	if len(os.Args) > 1 && os.Args[1] == "maintain-model-cache" {
		if err := maintainModelCache(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
			log.Fatal(err)
		}
		return
	}

	if len(os.Args) > 1 && os.Args[1] == "manage-deployments" {
		if err := manageDeployments(os.Args[2:]); err != nil && !errors.Is(err, flag.ErrHelp) {
			log.Fatal(err)
		}
		return
	}

	var deploymentUID string
	var metricsAddress string
	var probeAddress string
	var leaderElection bool
	var sourceMode bool
	var frontendEnabled bool
	var frontendMode string
	var frontendImage string
	var frontendApplicationURL string
	var modelServerApplicationURL string
	var videoWorkerImage string
	var videoWorkerApplicationURL string
	var applicationFilesJSON string
	var frontendPort int
	var frontendGatewayName string
	var frontendGatewayNamespace string
	var frontendGatewaySectionName string
	var inferenceEngineImage string
	var omniInferenceEngineImage string
	var nsightImage string
	var modelServerPort int
	var gpuResourceName string
	var runtimeClassName string
	var gpuNodeSelectorKey string
	var gpuNodeSelectorValue string
	var profiles runtimeconfig.Profiles
	var autoscalingTelemetryConfigurationPath string
	var workloadImagePullSecretNames []string
	var cacheClaimName string
	var cacheMountPath string
	cacheDirectoryBindings := runtimeconfig.RuntimeCacheDirectoryBindings{}
	var admissionStoreURLSecretName string
	var admissionStoreURLSecretKey string
	var responseStoreURLSecretName string
	var responseStoreURLSecretKey string
	var responseRetention string
	var responseReplayRetention string
	var modelSourceEndpoint string
	var modelSourceTokenSecretName string
	var modelSourceTokenSecretKey string
	var modelDownloadProxyURL string
	var observabilityPrometheus string
	var observabilityLabelsJSON string

	flag.StringVar(&deploymentUID, "deployment-uid", "", "Immutable deployment UID selecting this controller's resources.")
	// Metrics stay disabled until the chart exposes a secured endpoint.
	flag.StringVar(&metricsAddress, "metrics-bind-address", "0", "Metrics endpoint bind address; 0 disables metrics.")
	flag.StringVar(&probeAddress, "health-probe-bind-address", "0", "Health probe bind address; 0 disables probes.")
	flag.BoolVar(&leaderElection, "leader-elect", false, "Enable leader election.")
	flag.BoolVar(&sourceMode, "source-mode", false, "Enable service source bundles from the platform application origin.")
	flag.StringVar(&observabilityPrometheus, "observability-prometheus", "", "Prometheus NAMESPACE/NAME selected for service alert rules.")
	flag.StringVar(&observabilityLabelsJSON, "observability-labels", "{}", "JSON labels used to select controller-owned alert rules.")
	flag.StringVar(&autoscalingTelemetryConfigurationPath, "autoscaling-telemetry-config", "", "Platform configuration file supplying autoscaling telemetry collection budgets.")
	flag.BoolVar(&frontendEnabled, "frontend-enabled", false, "Enable FrontendService workload reconciliation.")
	flag.StringVar(&frontendMode, "frontend-mode", frontendModeLocal, "Frontend access mode: local or gateway.")
	flag.StringVar(&frontendImage, "frontend-image", "", "Frontend runtime image.")
	flag.StringVar(&frontendApplicationURL, "frontend-application-url", "", "Trusted platform frontend publication selected on deployment.")
	flag.StringVar(&modelServerApplicationURL, "model-server-application-url", "", "Trusted platform model-server publication selected on deployment.")
	flag.StringVar(&videoWorkerImage, "video-worker-image", "", "Platform runtime image for video workers.")
	flag.StringVar(&videoWorkerApplicationURL, "video-worker-application-url", "", "Published application directory containing the video worker.")
	flag.StringVar(&applicationFilesJSON, "application-files", "{}", "JSON configuration for platform application download tools.")
	flag.IntVar(&frontendPort, "frontend-port", 0, "Frontend runtime HTTP port supplied by the platform.")
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
	flag.StringVar(&cacheClaimName, "cache-claim", "", "Existing namespace-local PVC shared by runtime workloads.")
	flag.StringVar(&cacheMountPath, "cache-mount-path", "", "Absolute runtime cache root supplied by the platform.")
	flag.Func("cache-directory-bindings", "JSON mapping of authorized data directories to prepared namespace claims; an empty object authorizes no directories.", func(value string) error {
		if err := json.Unmarshal([]byte(value), &cacheDirectoryBindings); err != nil {
			return err
		}
		if cacheDirectoryBindings == nil {
			return errors.New("cache-directory-bindings must be a JSON object")
		}
		for directory, claim := range cacheDirectoryBindings {
			if directory == "" || claim == "" {
				return errors.New("cache-directory-bindings requires nonempty directories and claim names")
			}
		}
		return nil
	})
	flag.StringVar(&admissionStoreURLSecretName, "admission-store-url-secret-name", "", "Namespace-local Secret containing the shared admission ledger URL.")
	flag.StringVar(&admissionStoreURLSecretKey, "admission-store-url-secret-key", "", "Key in the admission ledger URL Secret.")
	flag.StringVar(&responseStoreURLSecretName, "response-store-url-secret-name", "", "Namespace-local Secret containing the shared API cache URL.")
	flag.StringVar(&responseStoreURLSecretKey, "response-store-url-secret-key", "", "Key in the shared API cache URL Secret.")
	flag.StringVar(&responseRetention, "response-retention", "", "Retention for stored responses and chat completions, such as 30d.")
	flag.StringVar(&responseReplayRetention, "response-replay-retention", "", "Retention for response stream replay and temporary background results, such as 10m.")
	flag.StringVar(&modelSourceEndpoint, "model-source-endpoint", "", "Optional Hugging Face-compatible Hub endpoint.")
	flag.StringVar(&modelSourceTokenSecretName, "model-source-token-secret-name", "", "Namespace-local Secret containing the Hugging Face credential.")
	flag.StringVar(&modelSourceTokenSecretKey, "model-source-token-secret-key", "", "Key in the model source credential Secret.")
	flag.StringVar(&modelDownloadProxyURL, "model-distribution-download-proxy", "", "Optional internal HTTP proxy for model preparation.")
	flag.StringVar(&inferenceEngineImage, "inference-engine-image", "", "Inference engine image containing the Foretoken model-server adapter.")
	flag.StringVar(&omniInferenceEngineImage, "omni-inference-engine-image", "", "Optional vLLM-Omni image containing the Foretoken Omni adapter.")
	flag.StringVar(&nsightImage, "nsight-image", "", "Optional NVIDIA model-server image prepared for Nsight Systems.")
	flag.IntVar(&modelServerPort, "model-server-port", 0, "Internal model-server HTTP port supplied by the platform.")
	flag.StringVar(&gpuResourceName, "gpu-resource-name", "", "Kubernetes extended resource selected by the platform for accelerator devices.")
	flag.StringVar(&runtimeClassName, "runtime-class-name", "", "Optional RuntimeClass for inference engine Pods.")
	flag.StringVar(&gpuNodeSelectorKey, "gpu-node-selector-key", "", "Optional GPU node label key.")
	flag.StringVar(&gpuNodeSelectorValue, "gpu-node-selector-value", "", "Optional GPU node label value.")
	flag.StringVar(&profiles.EC.Name, "vllm-ec-profile-name", "", "Platform-owned EC profile name; empty disables E/P/D.")
	flag.StringVar(&profiles.EC.Revision, "vllm-ec-profile-revision", "", "Platform-owned EC profile revision.")
	flag.StringVar(&profiles.EC.Connector, "vllm-ec-connector", "", "Fixed EC connector identity for the configured vLLM adapter.")
	flag.StringVar(&profiles.EC.SharedStorageClaim, "vllm-ec-shared-storage-claim", "", "ReadWriteMany PVC used by the configured EC connector.")
	flag.StringVar(&profiles.EC.SharedStoragePath, "vllm-ec-shared-storage-path", "", "In-container shared EC storage path supplied by the platform.")
	flag.StringVar(&profiles.PD.Name, "vllm-pd-profile-name", "", "Opaque platform-owned Mooncake P/D profile name; empty disables P/D.")
	flag.StringVar(&profiles.PD.Revision, "vllm-pd-profile-revision", "", "Opaque platform-owned Mooncake P/D profile revision.")
	flag.StringVar(&profiles.PD.Protocol, "vllm-pd-protocol", "", "Mooncake P/D transport: rdma or tcp.")
	flag.IntVar(&profiles.PD.BootstrapPort, "vllm-pd-bootstrap-port", 0, "Mooncake bootstrap port.")
	flag.IntVar(&profiles.PD.AbortRequestTimeoutSeconds, "vllm-pd-abort-request-timeout-seconds", 0, "Mooncake abort request timeout in seconds.")
	flag.StringVar(&profiles.PD.RDMADeviceName, "vllm-pd-rdma-device-name", "", "Optional comma-separated HCA filter; empty lets Mooncake select allocated devices by topology.")
	flag.StringVar(&profiles.RDMA.ResourceName, "rdma-resource-name", "", "Kubernetes extended resource that injects runtime RDMA devices.")
	flag.IntVar(&profiles.RDMA.ResourceCount, "rdma-resource-count", 0, "RDMA device-plugin allocation units requested by each distributed runtime Pod.")
	flag.StringVar(&profiles.MooncakeStore.Name, "vllm-mooncake-store-profile-name", "", "Opaque platform-owned Mooncake Store profile name; empty disables external Store.")
	flag.StringVar(&profiles.MooncakeStore.Revision, "vllm-mooncake-store-profile-revision", "", "Opaque platform-owned Mooncake Store profile revision.")
	flag.StringVar(&profiles.MooncakeStore.ConfigMapName, "vllm-mooncake-store-config-map-name", "", "Mooncake Store ConfigMap name.")
	flag.StringVar(&profiles.MooncakeStore.ConfigMapKey, "vllm-mooncake-store-config-map-key", "", "Mooncake Store ConfigMap key.")
	flag.StringVar(&profiles.MooncakeStore.PythonHashSeed, "vllm-mooncake-store-python-hash-seed", "", "PYTHONHASHSEED for Mooncake Store.")

	logOptions := zap.Options{Development: false}
	logOptions.BindFlags(flag.CommandLine)
	flag.Parse()
	workloadImagePullSecrets := make([]corev1.LocalObjectReference, len(workloadImagePullSecretNames))
	for index, name := range workloadImagePullSecretNames {
		workloadImagePullSecrets[index] = corev1.LocalObjectReference{Name: name}
	}
	cacheProfile := runtimeconfig.RuntimeCacheProfile{ClaimName: cacheClaimName, MountPath: cacheMountPath, DirectoryBindings: cacheDirectoryBindings}
	huggingFaceAccessProfile := runtimeconfig.HuggingFaceAccessProfile{Endpoint: modelSourceEndpoint, TokenSecretName: modelSourceTokenSecretName, TokenSecretKey: modelSourceTokenSecretKey}
	modelDistributionProfile := runtimeconfig.ModelDistributionProfile{ProxyURL: modelDownloadProxyURL}
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&logOptions)))
	admissionStore := &inferencev1alpha1.RedisConnection{URL: os.Getenv(runtimeconfig.AdmissionStoreURLEnv)}
	if admissionStoreURLSecretName != "" || admissionStoreURLSecretKey != "" {
		admissionStore.URLSecretRef = &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: admissionStoreURLSecretName},
			Key:                  admissionStoreURLSecretKey,
		}
	}
	if err := runtimeconfig.ValidateRedisConnection(admissionStore); err != nil {
		ctrl.Log.Error(err, "invalid admission store configuration")
		os.Exit(1)
	}
	var responseStore *inferencev1alpha1.ResponseStoreConfig
	if frontendEnabled {
		connection := inferencev1alpha1.RedisConnection{URL: os.Getenv(runtimeconfig.ResponseStoreURLEnv)}
		if responseStoreURLSecretName != "" || responseStoreURLSecretKey != "" {
			connection.URLSecretRef = &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: responseStoreURLSecretName},
				Key:                  responseStoreURLSecretKey,
			}
		}
		var err error
		responseStore, err = runtimeconfig.ResponseStoreConfiguration(connection, responseRetention, responseReplayRetention)
		if err != nil {
			ctrl.Log.Error(err, "invalid response store configuration")
			os.Exit(1)
		}
	}
	var applicationFiles runtimeconfig.ApplicationFiles
	if err := json.Unmarshal([]byte(applicationFilesJSON), &applicationFiles); err != nil {
		ctrl.Log.Error(err, "invalid application file configuration")
		os.Exit(1)
	}
	if applicationFiles.Image == "" {
		ctrl.Log.Error(errors.New("application-files image must be nonempty"), "invalid platform tools configuration")
		os.Exit(1)
	}
	if (sourceMode || videoWorkerApplicationURL != "" || frontendApplicationURL != "" || modelServerApplicationURL != "") && (applicationFiles.Script == "" || applicationFiles.MountPath == "" || (sourceMode && applicationFiles.Origin == "")) {
		ctrl.Log.Error(errors.New("source-mode and video-worker-application-url require application-files; source-mode also requires its origin"), "invalid application file configuration")
		os.Exit(1)
	}
	if inferenceEngineImage == "" {
		ctrl.Log.Error(errors.New("inference-engine-image must be nonempty"), "invalid inference engine profile")
		os.Exit(1)
	}
	if err := cacheProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid runtime cache profile")
		os.Exit(1)
	}
	if err := huggingFaceAccessProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid Hugging Face access profile")
		os.Exit(1)
	}
	if err := modelDistributionProfile.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid model distribution profile")
		os.Exit(1)
	}
	if modelServerPort < 1 || modelServerPort > 65535 {
		ctrl.Log.Error(errors.New("model-server-port must be between 1 and 65535"), "invalid inference engine profile")
		os.Exit(1)
	}
	if frontendMode != frontendModeLocal && frontendMode != frontendModeGateway {
		ctrl.Log.Error(errors.New("frontend-mode must be local or gateway"), "invalid frontend profile")
		os.Exit(1)
	}
	if err := profiles.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid vLLM runtime profile")
		os.Exit(1)
	}
	var rdma *inferencev1alpha1.RDMAAllocation
	if profiles.RDMA.ResourceName != "" {
		rdma = &inferencev1alpha1.RDMAAllocation{ResourceName: profiles.RDMA.ResourceName, ResourceCount: int32(profiles.RDMA.ResourceCount)}
	}
	var mooncakePD *runtimeconfig.PDProfile
	var ec *runtimeconfig.ECProfile
	var mooncakeStore *runtimeconfig.MooncakeStoreProfile
	if profiles.EC.Name != "" {
		ec = &profiles.EC
	}
	if profiles.MooncakeStore.Name != "" {
		mooncakeStore = &profiles.MooncakeStore
	}
	if profiles.PD.Name != "" {
		mooncakePD = &profiles.PD
	}

	// Register built-in and Foretoken APIs before controllers are attached to the manager.
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(inferencev1alpha1.AddToScheme(scheme))
	utilruntime.Must(monitoringv1.AddToScheme(scheme))
	utilruntime.Must(gatewayv1.Install(scheme))
	utilruntime.Must(lwsv1.AddToScheme(scheme))

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

	controlPlaneNamespace := os.Getenv("POD_NAMESPACE")
	if controlPlaneNamespace == "" || deploymentUID == "" {
		ctrl.Log.Error(errors.New("POD_NAMESPACE and --deployment-uid are required"), "unable to configure deployment controller scope")
		os.Exit(1)
	}
	scope := deployments.Scope{Namespace: controlPlaneNamespace, UID: deploymentUID}
	// Instance membership applies to watches and both client paths. Shared claims
	// remain observable so binding or expansion can wake their instance consumers.
	manager, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				controlPlaneNamespace: {LabelSelector: scope.Selector()},
			},
			ByObject: map[client.Object]cache.ByObject{
				&corev1.PersistentVolumeClaim{}: {Namespaces: map[string]cache.Config{
					controlPlaneNamespace: {LabelSelector: labels.Everything()},
				}},
			},
		},
		Client: client.Options{Cache: &client.CacheOptions{
			// Namespace credentials are read by fixed names without a Secret informer.
			DisableFor: []client.Object{&corev1.Secret{}},
		}},
		Metrics:                 metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress:  probeAddress,
		LeaderElection:          leaderElection,
		LeaderElectionID:        deployments.ResourceName(deploymentUID),
		LeaderElectionNamespace: controlPlaneNamespace,
		// Main exits when the manager stops, so a normal shutdown can release the lease.
		LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}
	manager = scope.Manager(manager)

	var observabilityLabels map[string]string
	if err := json.Unmarshal([]byte(observabilityLabelsJSON), &observabilityLabels); err != nil {
		ctrl.Log.Error(err, "invalid observability labels")
		os.Exit(1)
	}
	serviceAlerts, err := observability.NewServiceAlerts(manager, observabilityPrometheus, observabilityLabels)
	if err != nil {
		ctrl.Log.Error(err, "invalid observability configuration")
		os.Exit(1)
	}

	// Controllers are registered explicitly so each resource keeps one lifecycle owner.
	if err := (&profiling.Reconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ProfileRun controller")
		os.Exit(1)
	}
	if err := (&video.Reconciler{Client: manager.GetClient(), WorkerImage: videoWorkerImage, WorkerApplicationURL: videoWorkerApplicationURL, ApplicationFiles: applicationFiles, FrontendPort: int32(frontendPort), ImagePullSecrets: workloadImagePullSecrets}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register VideoTask controller")
		os.Exit(1)
	}
	if err := (&storage.RuntimeCacheReconciler{Client: manager.GetClient(), Profile: cacheProfile}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register RuntimeCache controller")
		os.Exit(1)
	}
	if frontendEnabled {
		var gateway *frontend.GatewayParent
		if frontendMode == frontendModeGateway {
			gateway = &frontend.GatewayParent{
				Name:        frontendGatewayName,
				Namespace:   frontendGatewayNamespace,
				SectionName: frontendGatewaySectionName,
			}
		}
		frontendReconciler := &frontend.Reconciler{
			Client:       manager.GetClient(),
			APIReader:    manager.GetAPIReader(),
			CacheProfile: cacheProfile,
			Alerts:       serviceAlerts,
			RuntimeProfile: frontend.FrontendRuntimeProfile{
				SourceMode:        sourceMode,
				ApplicationFiles:  applicationFiles,
				Image:             frontendImage,
				ApplicationURL:    frontendApplicationURL,
				WorkerImage:       videoWorkerImage,
				Port:              int32(frontendPort),
				ImagePullSecrets:  workloadImagePullSecrets,
				HuggingFaceAccess: huggingFaceAccessProfile.Access(),
				AdmissionStore:    admissionStore,
				ResponseStore:     responseStore,
				Gateway:           gateway,
			},
		}
		if err := frontendReconciler.SetupWithManager(manager); err != nil {
			ctrl.Log.Error(err, "unable to register FrontendService controller")
			os.Exit(1)
		}
	}
	metricsProvider, err := model.NewHTTPScalingMetricsProvider(manager.GetClient(), autoscalingTelemetryConfigurationPath)
	if err != nil {
		ctrl.Log.Error(err, "invalid autoscaling telemetry configuration")
		os.Exit(1)
	}
	if err := (&model.ServiceReconciler{
		Client:                   manager.GetClient(),
		CacheProfile:             cacheProfile,
		SourceMode:               sourceMode,
		RuntimeProfile:           resolver.RuntimeProfile{Image: inferenceEngineImage, OmniImage: omniInferenceEngineImage, NsightImage: nsightImage, AdmissionStore: admissionStore},
		ApplicationFiles:         applicationFiles,
		ApplicationURL:           modelServerApplicationURL,
		HuggingFaceAccessProfile: huggingFaceAccessProfile,
		Alerts:                   serviceAlerts,
		MetricsProvider:          metricsProvider,
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelService controller")
		os.Exit(1)
	}
	if err := (&kv.ServiceReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVService controller")
		os.Exit(1)
	}
	if err := (&kv.PoolReconciler{Client: manager.GetClient()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVPool controller")
		os.Exit(1)
	}
	if err := (&kv.GroupReconciler{Client: manager.GetClient(), HTTPClient: &http.Client{Timeout: 2 * time.Second}, ControlPlaneNamespace: controlPlaneNamespace}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register KVGroup controller")
		os.Exit(1)
	}
	if err := (&model.PoolReconciler{
		Client: manager.GetClient(),
		RuntimeProfile: resolver.RuntimeProfile{
			Image:              inferenceEngineImage,
			OmniImage:          omniInferenceEngineImage,
			NsightImage:        nsightImage,
			ModelServerPort:    int32(modelServerPort),
			DeviceResourceName: gpuResourceName,
			RuntimeClassName:   runtimeClassName,
			NodeSelectorKey:    gpuNodeSelectorKey,
			NodeSelectorValue:  gpuNodeSelectorValue,
			MooncakePD:         mooncakePD,
			RDMA:               rdma,
			EC:                 ec,
			MooncakeStore:      mooncakeStore,
		},
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "unable to register ModelPool controller")
		os.Exit(1)
	}
	if err := (&model.GroupReconciler{Client: manager.GetClient(), ControlPlaneNamespace: controlPlaneNamespace, ImagePullSecrets: workloadImagePullSecrets, ModelDistribution: modelDistributionProfile, ApplicationFiles: applicationFiles}).SetupWithManager(manager); err != nil {
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
