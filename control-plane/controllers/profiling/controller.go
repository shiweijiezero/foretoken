// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Coordinates diagnostic captures against a fixed, persisted runtime cohort.

package profiling

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"path"
	"reflect"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const profileFinalizer = "inference.foretoken.io/profile-stop"
const profilePath = "/v1/internal/profiling"
const profileLegacyArtifactRoot = "profiles/runs"

// Reconciler owns capture coordination, never serving resources or artifact storage.
type Reconciler struct {
	client.Client
	APIReader  client.Reader
	HTTPClient *http.Client
}

type profileRecord struct {
	RunUID           string  `json:"runUid"`
	NativeStopped    *bool   `json:"nativeStopped"`
	ExportedAtUnixMS *int64  `json:"exportedAtUnixMs"`
	Phase            string  `json:"phase"`
	StartedAtUnixMS  *int64  `json:"startedAtUnixMs"`
	ArtifactDir      *string `json:"artifactDir"`
	Message          string  `json:"message"`
	GPUActivity      *bool   `json:"gpuActivity"`
}

type profileObservation struct {
	Protocol          int            `json:"protocol"`
	Engine            string         `json:"engine"`
	RuntimeID         string         `json:"runtimeId"`
	PodUID            string         `json:"podUid"`
	GroupUID          string         `json:"groupUid"`
	RuntimeCacheClaim string         `json:"runtimeCacheClaim"`
	Capture           *profileRecord `json:"capture"`
}

// profileCaptureProgress summarizes one ordered pass over the fixed capture participants.
type profileCaptureProgress struct {
	action       string
	allDone      bool
	anyStopping  bool
	capturing    int
	completed    int32
	gpuActive    int
	hasArtifacts bool
}

// Reconcile persists recovery state before starting work, then observes the same runtime identities.
func (r *Reconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	run := new(inferencev1alpha1.ProfileRun)
	if err := r.APIReader.Get(ctx, request.NamespacedName, run); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if profileTerminal(run.Status.Phase) {
		return ctrl.Result{}, r.removeProfileFinalizer(ctx, run)
	}
	if run.Status.Plan == nil {
		if run.Status.Phase != "" {
			return ctrl.Result{}, fmt.Errorf("capture recovery plan is missing; refusing to reselect runtimes")
		}
		if run.Spec.Action != "Capture" || !run.DeletionTimestamp.IsZero() {
			run.Status.Phase = "Cancelled"
			return ctrl.Result{}, r.writeProfileStatus(ctx, run)
		}
		if !controllerutil.ContainsFinalizer(run, profileFinalizer) {
			base := run.DeepCopy()
			controllerutil.AddFinalizer(run, profileFinalizer)
			return ctrl.Result{Requeue: true}, r.Patch(ctx, run, client.MergeFrom(base))
		}
		plan, err := r.prepareProfile(ctx, run)
		if err != nil {
			run.Status.Phase, run.Status.Reason, run.Status.Message = "Failed", "TargetUnavailable", err.Error()
			return ctrl.Result{}, r.writeProfileStatus(ctx, run)
		}
		run.Status.Plan = &plan
		run.Status.Phase = "Pending"
		// Persist identities before protecting Pods and accepting native operations.
		return ctrl.Result{Requeue: true}, r.writeProfileStatus(ctx, run)
	}
	plan := *run.Status.Plan
	if plan.ArtifactDirectory == "" {
		// Plans persisted before scoped capture storage keep their original directory.
		plan.ArtifactDirectory = path.Join(profileLegacyArtifactRoot, string(run.UID))
	}

	action := run.Spec.Action
	if !run.DeletionTimestamp.IsZero() || run.Status.Reason != "" {
		action = "Cancel"
	}
	service := new(inferencev1alpha1.ModelService)
	err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: run.Spec.ModelServiceRef.Name}, service)
	if err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	if apierrors.IsNotFound(err) || string(service.UID) != plan.ServiceUID || service.Status.ServingGeneration != plan.ServingGeneration || !reflect.DeepEqual(service.Status.ServingPoolRevisions, plan.Revisions) || !service.DeletionTimestamp.IsZero() {
		if run.Status.Reason == "" {
			run.Status.Reason, run.Status.Message = "CoverageChanged", "selected serving generation is no longer active"
		}
		action = "Cancel"
	}
	unchanged, err := r.profileCohortUnchanged(ctx, run.Namespace, plan)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !unchanged {
		if run.Status.Reason == "" {
			run.Status.Reason, run.Status.Message = "CoverageChanged", "selected runtime cohort changed during capture"
		}
		action = "Cancel"
	}
	if run.Status.Phase == "Pending" {
		return ctrl.Result{Requeue: true}, r.protectProfileMembers(ctx, run)
	}
	pods, err := r.observeProfileMembers(ctx, run)
	if err != nil {
		return ctrl.Result{}, err
	}
	if run.Status.Reason != "" {
		action = "Cancel"
	}
	duration, err := time.ParseDuration(string(run.Spec.Duration))
	if err != nil || duration < time.Millisecond {
		return ctrl.Result{}, fmt.Errorf("capture duration must be at least 1ms: %q", run.Spec.Duration)
	}
	progress, err := r.reconcileCaptureParticipants(ctx, run, plan, pods, action, duration)
	if err != nil {
		return ctrl.Result{}, err
	}
	run.Status.Participants, run.Status.CompletedParticipants = int32(len(plan.Participants)), progress.completed
	run.Status.Phase = "Starting"
	if progress.capturing == len(plan.Participants) {
		run.Status.Phase = "Capturing"
	}
	if progress.anyStopping || progress.action != "Capture" {
		run.Status.Phase = "Stopping"
	}
	if progress.allDone {
		now := metav1.Now()
		run.Status.FinishedAt = &now
		switch {
		case run.Status.Reason != "":
			run.Status.Phase = "Failed"
		case progress.action == "Cancel":
			run.Status.Phase = "Cancelled"
		default:
			run.Status.Phase = "Succeeded"
			run.Status.Message = fmt.Sprintf("GPU kernel activity recorded on %d/%d runtimes", progress.gpuActive, len(plan.Participants))
		}
		if progress.hasArtifacts {
			run.Status.Artifact = &inferencev1alpha1.ProfileArtifactReference{ClaimName: plan.RuntimeCacheClaim, Path: plan.ArtifactDirectory}
		}
	}
	return ctrl.Result{RequeueAfter: time.Second}, r.writeProfileStatus(ctx, run)
}

// reconcileCaptureParticipants advances the fixed cohort and returns its observed progress.
// A participant failure cancels later operations in this pass without replacing the first failure.
func (r *Reconciler) reconcileCaptureParticipants(ctx context.Context, run *inferencev1alpha1.ProfileRun, plan inferencev1alpha1.ProfileExecutionPlan, pods map[string]*corev1.Pod, action string, duration time.Duration) (profileCaptureProgress, error) {
	progress := profileCaptureProgress{action: action, allDone: true}
	for _, participant := range plan.Participants {
		if profileParticipantStopped(run, participant) {
			progress.action = "Cancel"
			continue
		}
		pod := pods[participant.PodUID]
		if pod == nil || pod.Status.Phase == corev1.PodFailed || pod.Status.Phase == corev1.PodSucceeded {
			progress.allDone = false
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "StopUnconfirmed", "waiting for the selected engine containers to stop"
			}
			progress.action = "Cancel"
			continue
		}
		observation, err := r.profileHTTP(ctx, participant.Endpoint, string(run.UID), nil)
		if err != nil {
			// Loss of contact is not evidence that an engine stopped. Keep the finalizer and plan.
			progress.allDone = false
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "RuntimeUnreachable", err.Error()
			}
			progress.action = "Cancel"
			continue
		}
		if observation.RuntimeID != participant.RuntimeID || observation.PodUID != participant.PodUID || observation.GroupUID != participant.GroupUID || observation.RuntimeCacheClaim != plan.RuntimeCacheClaim {
			progress.allDone = false
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "RuntimeReplaced", "the selected engine process was replaced"
			}
			progress.action = "Cancel"
			continue
		}
		if observation.Capture == nil || (!profileTerminal(observation.Capture.Phase) && progress.action != "Capture") {
			operation := struct {
				RunUID     string `json:"runUid"`
				RuntimeID  string `json:"runtimeId"`
				GroupUID   string `json:"groupUid"`
				Action     string `json:"action"`
				DurationMS int64  `json:"durationMs"`
				Engine     string `json:"engine,omitempty"`
			}{string(run.UID), participant.RuntimeID, participant.GroupUID, progress.action, duration.Milliseconds(), ""}
			// Existing PyTorch runtimes predate the engine field and reject unknown fields.
			if run.Spec.Engine != "pytorch" {
				operation.Engine = run.Spec.Engine
			}
			observation, err = r.profileHTTP(ctx, participant.Endpoint, string(run.UID), operation)
			if err != nil {
				progress.allDone = false
				if run.Status.Reason == "" {
					run.Status.Reason, run.Status.Message = "ControlFailed", err.Error()
				}
				progress.action = "Cancel"
				continue
			}
		}
		record := observation.Capture
		if record == nil || record.RunUID != string(run.UID) {
			return profileCaptureProgress{}, fmt.Errorf("runtime returned an invalid capture identity")
		}
		expectedArtifact := path.Join(plan.ArtifactDirectory, participant.RuntimeID)
		if record.ArtifactDir != nil && *record.ArtifactDir == expectedArtifact {
			progress.hasArtifacts = true
		}
		if record.StartedAtUnixMS != nil {
			started := metav1.NewTime(time.UnixMilli(*record.StartedAtUnixMS))
			if run.Status.StartedAt == nil || started.Before(run.Status.StartedAt) {
				run.Status.StartedAt = &started
			}
		}
		if profileTerminal(record.Phase) {
			nativeStopped := record.Phase == "Succeeded" || record.Phase == "Cancelled" || record.ExportedAtUnixMS != nil
			if record.NativeStopped != nil {
				nativeStopped = *record.NativeStopped
			}
			if !nativeStopped {
				progress.allDone = false
			}
		}
		switch record.Phase {
		case "Succeeded":
			if record.ArtifactDir == nil || *record.ArtifactDir != expectedArtifact {
				if run.Status.Reason == "" {
					run.Status.Reason, run.Status.Message = "InvalidArtifact", "runtime did not publish the expected artifact reference"
				}
				progress.action = "Cancel"
			} else {
				progress.completed++
				if record.GPUActivity != nil && *record.GPUActivity {
					progress.gpuActive++
				}
			}
		case "Cancelled", "Failed":
			if record.Phase == "Failed" && run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "CaptureFailed", record.Message
			}
			progress.action = "Cancel"
		default:
			progress.allDone = false
			if record.Phase == "Capturing" {
				progress.capturing++
			}
			progress.anyStopping = progress.anyStopping || record.Phase == "Stopping"
		}
	}
	return progress, nil
}

// profileCohortUnchanged detects scale/replacement changes without retargeting the fixed execution plan.
func (r *Reconciler) profileCohortUnchanged(ctx context.Context, namespace string, plan inferencev1alpha1.ProfileExecutionPlan) (bool, error) {
	groups := new(inferencev1alpha1.ModelGroupList)
	if err := r.APIReader.List(ctx, groups, client.InNamespace(namespace)); err != nil {
		return false, err
	}
	current := map[string]bool{}
	for _, group := range groups.Items {
		for _, revision := range plan.Revisions {
			if group.Spec.ModelPoolRef.UID == revision.PoolUID && group.Spec.Revision == revision.Revision && servingconfig.RoutingGroupReady(&group) {
				current[string(group.UID)] = true
			}
		}
	}
	expected := map[string]bool{}
	for _, participant := range plan.Participants {
		expected[participant.GroupUID] = true
	}
	return reflect.DeepEqual(current, expected), nil
}

// prepareProfile resolves the committed serving cohort without introducing a second routing policy.
func (r *Reconciler) prepareProfile(ctx context.Context, run *inferencev1alpha1.ProfileRun) (inferencev1alpha1.ProfileExecutionPlan, error) {
	var plan inferencev1alpha1.ProfileExecutionPlan
	service := new(inferencev1alpha1.ModelService)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: run.Spec.ModelServiceRef.Name}, service); err != nil {
		return plan, err
	}
	if !servingconfig.HasServingCohort(service) || !meta.IsStatusConditionTrue(service.Status.Conditions, workload.ConditionReady) {
		return plan, fmt.Errorf("ModelService must already be Ready")
	}
	plan.ServiceUID, plan.ServingGeneration, plan.Revisions = string(service.UID), service.Status.ServingGeneration, service.Status.ServingPoolRevisions
	pools, groups := new(inferencev1alpha1.ModelPoolList), new(inferencev1alpha1.ModelGroupList)
	if err := r.APIReader.List(ctx, pools, client.InNamespace(run.Namespace)); err != nil {
		return plan, err
	}
	if err := r.APIReader.List(ctx, groups, client.InNamespace(run.Namespace)); err != nil {
		return plan, err
	}
	coveredPools := 0
	for _, pool := range pools.Items {
		if !servingconfig.RoutingPoolOwnedBy(&pool, service) || servingconfig.ServingRevision(service, &pool) == "" {
			continue
		}
		before := len(plan.Participants)
		for _, group := range groups.Items {
			if !servingconfig.RoutingGroupOwnedBy(&group, &pool) || group.Spec.Revision != servingconfig.ServingRevision(service, &pool) || !servingconfig.RoutingGroupReady(&group) {
				continue
			}
			// Desired service intent may already name the next model during a rollout.
			plan.Model = group.Spec.Artifacts.Model
			cache := group.Spec.Artifacts.Cache
			if cache == nil {
				return plan, fmt.Errorf("ModelService %s has no persistent RuntimeCache; add one to the deployment and redeploy before profiling", service.Name)
			}
			if plan.RuntimeCacheClaim != "" && plan.RuntimeCacheClaim != cache.ClaimName {
				return plan, fmt.Errorf("serving runtimes use different RuntimeCache claims")
			}
			plan.RuntimeCacheClaim = cache.ClaimName
			pods := new(corev1.PodList)
			if err := r.APIReader.List(ctx, pods, client.InNamespace(run.Namespace), client.MatchingLabels{workload.ModelGroupLabel: group.Name}); err != nil {
				return plan, err
			}
			selected := 0
			for _, pod := range pods.Items {
				if !workload.PodReady(&pod) || !pod.DeletionTimestamp.IsZero() {
					continue
				}
				if err := r.verifyProfilePod(ctx, &pod, &group); err != nil {
					return plan, err
				}
				directory, err := profileRunArtifactPath(&pod, cache.MountPath, string(run.UID), group.Spec.Runtime.PreparationVersion)
				if err != nil {
					return plan, err
				}
				if plan.ArtifactDirectory != "" && plan.ArtifactDirectory != directory {
					return plan, fmt.Errorf("serving runtimes use different profiling directory layouts; redeploy ModelService %s before profiling", service.Name)
				}
				plan.ArtifactDirectory = directory
				member := inferencev1alpha1.ProfileMember{GroupUID: string(group.UID), PodName: pod.Name, PodUID: string(pod.UID)}
				for _, container := range pod.Status.ContainerStatuses {
					if container.Name == "model-server" && container.State.Running != nil {
						member.ContainerName, member.ContainerID = container.Name, container.ContainerID
					}
				}
				if member.ContainerID == "" {
					return plan, fmt.Errorf("diagnostic Pod %s has no running engine container identity", pod.Name)
				}
				plan.Members = append(plan.Members, member)
				selected++
				// RPC tools are controlled by the leader for the entire Group;
				// each Nsight session owns only its Pod-local process tree.
				if run.Spec.Engine != "nsight" && group.Spec.NodeCount > 1 && pod.Labels[lwsv1.WorkerIndexLabelKey] != "0" {
					continue
				}
				endpoint := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(group.Spec.Runtime.Port)))
				observation, err := r.profileHTTP(ctx, endpoint, "", nil)
				if err != nil {
					return plan, fmt.Errorf("ModelGroup %s is not prepared for profiling: %w", group.Name, err)
				}
				if observation.Protocol != 1 {
					return plan, fmt.Errorf("ModelGroup %s requires an updated model-server application for profiling; redeploy the ModelService", group.Name)
				}
				if observation.GroupUID != string(group.UID) || observation.PodUID != string(pod.UID) || observation.RuntimeID == "" || observation.RuntimeCacheClaim != cache.ClaimName {
					return plan, fmt.Errorf("runtime returned an incomplete or mismatched RuntimeCache identity")
				}
				if observation.Engine != run.Spec.Engine {
					return plan, fmt.Errorf("runtime is prepared for %s, requested %s; set ModelService spec.profiling.engine and redeploy", observation.Engine, run.Spec.Engine)
				}
				plan.Participants = append(plan.Participants, inferencev1alpha1.ProfileParticipant{GroupName: group.Name, GroupUID: string(group.UID), PodName: pod.Name, PodUID: string(pod.UID), RuntimeID: observation.RuntimeID, Endpoint: endpoint})
			}
			if selected != int(group.Spec.MemberCount) {
				return plan, fmt.Errorf("ModelGroup %s does not have its expected Ready runtime members", group.Name)
			}
		}
		if len(plan.Participants) == before {
			return plan, fmt.Errorf("serving ModelPool %s has no Ready diagnostic runtimes", pool.Name)
		}
		coveredPools++
	}
	if coveredPools != len(plan.Revisions) || len(plan.Participants) == 0 {
		return plan, fmt.Errorf("not every committed serving pool has Ready diagnostic runtimes")
	}
	return plan, nil
}

// verifyProfilePod shares membership provenance and checks the selected diagnostic workload's topology.
func (r *Reconciler) verifyProfilePod(ctx context.Context, pod *corev1.Pod, group *inferencev1alpha1.ModelGroup) error {
	ownership, err := workload.ModelGroupForPod(ctx, r.APIReader, pod)
	if err != nil {
		return err
	}
	if ownership.Group.UID != group.UID {
		return fmt.Errorf("diagnostic Pod ownership does not match ModelGroup")
	}
	if group.Spec.NodeCount == 1 {
		deployment, ok := ownership.Workload.(*appsv1.Deployment)
		if !ok || deployment.Name != group.Name {
			return fmt.Errorf("diagnostic Pod ownership does not match ModelGroup Deployment")
		}
		return nil
	}

	executionWorkload, ok := ownership.Workload.(*lwsv1.LeaderWorkerSet)
	if !ok || executionWorkload.Name != workload.ModelGroupLeaderWorkerSetName(group) {
		return fmt.Errorf("diagnostic LeaderWorkerSet ownership does not match ModelGroup")
	}
	if executionWorkload.Spec.Replicas == nil || *executionWorkload.Spec.Replicas != 1 || executionWorkload.Spec.LeaderWorkerTemplate.Size == nil || *executionWorkload.Spec.LeaderWorkerTemplate.Size != group.Spec.MemberCount {
		return fmt.Errorf("diagnostic LeaderWorkerSet must contain one complete ModelGroup")
	}
	if pod.Labels[lwsv1.SetNameLabelKey] != executionWorkload.Name || pod.Labels[lwsv1.GroupIndexLabelKey] != "0" {
		return fmt.Errorf("diagnostic Pod identity does not match ModelGroup LeaderWorkerSet")
	}
	workerIndex, err := strconv.Atoi(pod.Labels[lwsv1.WorkerIndexLabelKey])
	if err != nil || workerIndex < 0 || int32(workerIndex) >= group.Spec.MemberCount {
		return fmt.Errorf("diagnostic Pod has an invalid LeaderWorkerSet worker index")
	}
	owner := metav1.GetControllerOf(pod)
	leader := ownership.Leader
	if workerIndex == 0 {
		if owner.Name != executionWorkload.Name || leader.UID != pod.UID {
			return fmt.Errorf("diagnostic leader Pod ownership does not match ModelGroup LeaderWorkerSet")
		}
		return nil
	}

	leaderName := pod.Annotations[lwsv1.LeaderPodNameAnnotationKey]
	if leaderName == "" || owner.Name != leaderName || leader.Name != leaderName || leader.UID == pod.UID {
		return fmt.Errorf("diagnostic worker Pod does not identify its LeaderWorkerSet leader")
	}
	if leader.Labels[workload.ModelGroupLabel] != group.Name || leader.Labels[lwsv1.SetNameLabelKey] != executionWorkload.Name || leader.Labels[lwsv1.GroupIndexLabelKey] != "0" || leader.Labels[lwsv1.WorkerIndexLabelKey] != "0" {
		return fmt.Errorf("diagnostic worker Pod leader identity does not match ModelGroup LeaderWorkerSet")
	}
	if metav1.GetControllerOf(leader).Name != executionWorkload.Name {
		return fmt.Errorf("diagnostic leader ownership does not match ModelGroup LeaderWorkerSet")
	}
	return nil
}

// profileHTTP uses the existing internal listener; write responses acknowledge acceptance, not completion.
func (r *Reconciler) profileHTTP(ctx context.Context, endpoint, uid string, operation any) (profileObservation, error) {
	var observation profileObservation
	method := http.MethodGet
	var body io.Reader
	if operation != nil {
		method = http.MethodPost
		data, err := json.Marshal(operation)
		if err != nil {
			return observation, err
		}
		body = bytes.NewReader(data)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint+profilePath+"?run_uid="+uid, body)
	if err != nil {
		return observation, err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := r.HTTPClient.Do(request)
	if err != nil {
		return observation, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotImplemented {
		return observation, fmt.Errorf("runtime profiling is unavailable; ensure the ModelService uses a writable persistent RuntimeCache and redeploy it")
	}
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusAccepted {
		return observation, fmt.Errorf("profiling endpoint returned HTTP %d", response.StatusCode)
	}
	err = json.NewDecoder(response.Body).Decode(&observation)
	return observation, err
}

func profileTerminal(phase string) bool {
	return phase == "Succeeded" || phase == "Failed" || phase == "Cancelled"
}

// profileRunArtifactPath retains the selected Pod's actual output layout in a new execution plan.
func profileRunArtifactPath(pod *corev1.Pod, dataRoot, runUID string, preparationVersion int32) (string, error) {
	if preparationVersion < runtimeconfig.SharedModelCachePreparationVersion {
		return path.Join(profileLegacyArtifactRoot, runUID), nil
	}
	for _, container := range pod.Spec.Containers {
		if container.Name != "model-server" {
			continue
		}
		for _, variable := range container.Env {
			if variable.Name != runtimeconfig.ProfileRootEnv {
				continue
			}
			root, within := strings.CutPrefix(path.Clean(variable.Value), path.Clean(dataRoot)+"/")
			if !within {
				return "", fmt.Errorf("diagnostic Pod %s has profiling storage outside its RuntimeCache", pod.Name)
			}
			return path.Join(root, "runs", runUID), nil
		}
	}
	return "", fmt.Errorf("diagnostic Pod %s has no profiling storage root", pod.Name)
}

// writeProfileStatus publishes only observed progress while preserving user-owned intent.
func (r *Reconciler) writeProfileStatus(ctx context.Context, run *inferencev1alpha1.ProfileRun) error {
	run.Status.ObservedGeneration = run.Generation
	return r.Status().Update(ctx, run)
}

// removeProfileFinalizer releases the recovery plan only after all possible starts have stopped.
func (r *Reconciler) removeProfileFinalizer(ctx context.Context, run *inferencev1alpha1.ProfileRun) error {
	if err := r.releaseProfileMembers(ctx, run); err != nil {
		return err
	}
	if !controllerutil.ContainsFinalizer(run, profileFinalizer) {
		return nil
	}
	base := run.DeepCopy()
	controllerutil.RemoveFinalizer(run, profileFinalizer)
	return r.Patch(ctx, run, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
}

// SetupWithManager registers the existing manager as the sole capture coordinator.
func (r *Reconciler) SetupWithManager(manager ctrl.Manager) error {
	r.APIReader = manager.GetAPIReader()
	r.HTTPClient = &http.Client{Timeout: 5 * time.Second}
	return ctrl.NewControllerManagedBy(manager).For(&inferencev1alpha1.ProfileRun{}).Complete(r)
}
