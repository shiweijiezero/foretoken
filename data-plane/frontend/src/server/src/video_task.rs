// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Deployment-scoped VideoTask access and streaming reads from dedicated result storage.

use std::collections::BTreeMap;
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use kube::api::{DeleteParams, DynamicObject, Patch, PatchParams, PostParams};
use kube::core::ApiResource;
use kube::{Api, Client, Config};
use serde::{Deserialize, Serialize};
use tokio_util::io::ReaderStream;
use uuid::Uuid;

static VIDEO_TASK_RESOURCE: LazyLock<ApiResource> = LazyLock::new(|| ApiResource {
    group: "inference.foretoken.io".into(),
    version: "v1alpha1".into(),
    api_version: "inference.foretoken.io/v1alpha1".into(),
    kind: "VideoTask".into(),
    plural: "videotasks".into(),
});

pub(crate) struct VideoTaskClient {
    tasks: Api<DynamicObject>,
    output_mount: PathBuf,
    frontend_uid: String,
    deployment_uid: String,
    worker_image: String,
    worker_endpoint: String,
    output_claim: String,
}

/// Generation parameters shared by task submission and the persisted VideoTask request.
#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct VideoTaskRequest {
    pub task: String,
    pub prompt: String,
    pub width: i32,
    pub height: i32,
    pub num_frames: i32,
    pub fps: i32,
    pub num_inference_steps: i32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub aspect_ratio: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flow_shift: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio_flow_shift: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seed: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub frame_indices: Vec<i32>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub input_files: Vec<VideoInputFile>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct VideoInputFile {
    pub field: String,
    pub path: String,
    pub content_type: String,
}

pub(crate) struct VideoTaskResource {
    pub id: String,
    pub status: VideoTaskStatus,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct VideoTaskStatus {
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub artifact: Option<VideoArtifact>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct VideoArtifact {
    pub claim_name: String,
    pub path: String,
}

impl VideoTaskClient {
    /// Initializes optional task access; invalid enabled configuration prevents a partially serving frontend.
    pub(crate) fn from_service_account() -> Result<Option<Arc<Self>>, Box<dyn std::error::Error>> {
        let output_mount = match std::env::var("FORETOKEN_VIDEO_TASK_OUTPUT_MOUNT") {
            Ok(path) => PathBuf::from(path),
            Err(std::env::VarError::NotPresent) => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        let mut config = Config::incluster()?;
        let namespace = config.default_namespace.clone();
        // Keep retries with the task owner and credentials out of HTTP proxies.
        config.default_retry = false;
        config.proxy_url = None;
        let client = Client::try_from(config)?;
        Ok(Some(Arc::new(Self {
            tasks: Api::namespaced_with(client, namespace.trim(), &*VIDEO_TASK_RESOURCE),
            output_mount: std::fs::canonicalize(output_mount)?,
            frontend_uid: std::env::var("FORETOKEN_VIDEO_TASK_FRONTEND_UID")?,
            deployment_uid: std::env::var("FORETOKEN_DEPLOYMENT_UID")?,
            worker_image: std::env::var("FORETOKEN_VIDEO_TASK_WORKER_IMAGE")?,
            worker_endpoint: std::env::var("FORETOKEN_VIDEO_TASK_ENDPOINT")?,
            output_claim: std::env::var("FORETOKEN_VIDEO_TASK_OUTPUT_CLAIM")?,
        })))
    }

    /// Persists generation intent and stages inputs, returning the task ID to the API adapter.
    pub(crate) async fn create(
        &self,
        model: &str,
        service: &crate::VideoServiceReference,
        mut request: VideoTaskRequest,
    ) -> Result<String, StatusCode> {
        let id = format!("video-{}", Uuid::new_v4());
        let mut sources = Vec::new();
        for (index, input) in request.input_files.iter_mut().enumerate() {
            if !Path::new(&input.path).starts_with("inputs") {
                return Err(StatusCode::BAD_REQUEST);
            }
            let source = self.storage_path(&input.path).await?;
            if !tokio::fs::metadata(&source)
                .await
                .map_err(|_| StatusCode::NOT_FOUND)?
                .is_file()
            {
                return Err(StatusCode::BAD_REQUEST);
            }
            // Media loaders may use the filename suffix in addition to Content-Type.
            let extension = Path::new(&input.path)
                .extension()
                .and_then(|value| value.to_str())
                .map(|value| format!(".{value}"))
                .unwrap_or_default();
            input.path = format!("tasks/{id}/input-{index}{extension}");
            sources.push((source, self.output_mount.join(&input.path)));
        }
        let inputs_ready = sources.is_empty();
        let mut body = DynamicObject::new(&id, &VIDEO_TASK_RESOURCE).data(serde_json::json!({
            "spec": {
                "frontendUID": self.frontend_uid,
                "inputsReady": inputs_ready,
                "model": model,
                "modelServiceRef": service,
                "request": request,
                "worker": {
                    "image": self.worker_image,
                    "endpoint": self.worker_endpoint,
                    "outputClaimName": self.output_claim,
                    "outputPath": format!("tasks/{id}/result.mp4"),
                },
            },
        }));
        body.metadata.labels = Some(BTreeMap::from([
            (
                "inference.foretoken.io/video-frontend-uid".into(),
                self.frontend_uid.clone(),
            ),
            (
                "inference.foretoken.io/deployment-uid".into(),
                self.deployment_uid.clone(),
            ),
        ]));
        task_api(self.tasks.create(&PostParams::default(), &body)).await?;
        // The durable task exists before any private file is written. Incomplete staging is
        // observable and expires through the same controller cleanup as finished generation.
        if !inputs_ready {
            let directory = self.output_mount.join(format!("tasks/{id}"));
            // The Pod prepares a setgid tasks root under the output-volume identity.
            // Share only this new task directory and its copies with the worker's group.
            tokio::fs::create_dir(&directory)
                .await
                .map_err(storage_error_status)?;
            tokio::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o2770))
                .await
                .map_err(storage_error_status)?;
            for (source, target) in sources {
                tokio::fs::copy(source, &target)
                    .await
                    .map_err(storage_error_status)?;
                tokio::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o640))
                    .await
                    .map_err(storage_error_status)?;
                tokio::fs::File::open(target)
                    .await
                    .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
                    .sync_all()
                    .await
                    .map_err(storage_error_status)?;
            }
            tokio::fs::File::open(directory)
                .await
                .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
                .sync_all()
                .await
                .map_err(storage_error_status)?;
            task_api(self.tasks.patch(
                &id,
                &PatchParams::default(),
                &Patch::Merge(serde_json::json!({"spec":{"inputsReady":true}})),
            ))
            .await?;
        }
        Ok(id)
    }

    /// Returns only tasks submitted through this FrontendService, across any of its replicas.
    pub(crate) async fn get(&self, id: &str) -> Result<VideoTaskResource, StatusCode> {
        validate_task_id(id)?;
        let task = task_api(self.tasks.get(id)).await?;
        let frontend_uid = task
            .data
            .get("spec")
            .and_then(|spec| spec.get("frontendUID"))
            .and_then(serde_json::Value::as_str)
            .ok_or(StatusCode::BAD_GATEWAY)?;
        if frontend_uid != self.frontend_uid
            || task
                .metadata
                .labels
                .as_ref()
                .and_then(|labels| labels.get("inference.foretoken.io/video-frontend-uid"))
                != Some(&self.frontend_uid)
        {
            return Err(StatusCode::NOT_FOUND);
        }
        Ok(VideoTaskResource {
            id: task.metadata.name.ok_or(StatusCode::BAD_GATEWAY)?,
            status: task
                .data
                .get("status")
                .cloned()
                .map(serde_json::from_value)
                .transpose()
                .map_err(|_| StatusCode::BAD_GATEWAY)?
                .unwrap_or_default(),
        })
    }

    /// Records cancellation intent; the controller confirms termination before publishing Cancelled.
    pub(crate) async fn cancel(&self, id: &str) -> Result<(), StatusCode> {
        self.get(id).await?;
        task_api(self.tasks.patch(
            id,
            &PatchParams::default(),
            &Patch::Merge(serde_json::json!({"spec":{"cancelRequested":true}})),
        ))
        .await?;
        Ok(())
    }

    /// Requests task deletion and controller-owned artifact cleanup.
    pub(crate) async fn delete(&self, id: &str) -> Result<(), StatusCode> {
        self.get(id).await?;
        task_api(self.tasks.delete(id, &DeleteParams::default())).await?;
        Ok(())
    }

    /// Streams completed video bytes without buffering the artifact into frontend memory.
    pub(crate) async fn content(&self, id: &str) -> Result<Body, StatusCode> {
        let task = self.get(id).await?;
        if task.status.phase != "Succeeded" {
            return Err(StatusCode::CONFLICT);
        }
        let artifact = task.status.artifact.ok_or(StatusCode::BAD_GATEWAY)?;
        if artifact.claim_name != self.output_claim
            || artifact.path != format!("tasks/{id}/result.mp4")
        {
            return Err(StatusCode::BAD_GATEWAY);
        }
        let path = self.storage_path(&artifact.path).await?;
        let file = tokio::fs::File::open(path)
            .await
            .map_err(storage_error_status)?;
        Ok(Body::from_stream(ReaderStream::with_capacity(
            file,
            64 * 1024,
        )))
    }

    /// Resolves existing files within the configured task volume, including symlink targets.
    async fn storage_path(&self, relative: &str) -> Result<PathBuf, StatusCode> {
        let path = Path::new(relative);
        if relative.is_empty()
            || path
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err(StatusCode::BAD_REQUEST);
        }
        let resolved = tokio::fs::canonicalize(self.output_mount.join(path))
            .await
            .map_err(storage_error_status)?;
        if !resolved.starts_with(&self.output_mount) {
            return Err(StatusCode::BAD_REQUEST);
        }
        Ok(resolved)
    }
}

/// Applies the total task API budget, including response decoding, without changing Kubernetes retry policy.
async fn task_api<T>(
    request: impl Future<Output = Result<T, kube::Error>>,
) -> Result<T, StatusCode> {
    tokio::time::timeout(Duration::from_secs(30), request)
        .await
        .map_err(|_| StatusCode::BAD_GATEWAY)?
        .map_err(|error| match error {
            kube::Error::Api(error) => match error.code {
                404 => StatusCode::NOT_FOUND,
                409 => StatusCode::CONFLICT,
                400 | 422 => StatusCode::BAD_REQUEST,
                429 => StatusCode::TOO_MANY_REQUESTS,
                _ => StatusCode::BAD_GATEWAY,
            },
            _ => StatusCode::BAD_GATEWAY,
        })
}

fn validate_task_id(id: &str) -> Result<(), StatusCode> {
    let value = id.strip_prefix("video-").ok_or(StatusCode::NOT_FOUND)?;
    Uuid::parse_str(value).map_err(|_| StatusCode::NOT_FOUND)?;
    Ok(())
}

// Keep denied external-file access distinct from missing files and storage failures.
fn storage_error_status(error: std::io::Error) -> StatusCode {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => StatusCode::FORBIDDEN,
        std::io::ErrorKind::NotFound => StatusCode::NOT_FOUND,
        _ => StatusCode::INSUFFICIENT_STORAGE,
    }
}
