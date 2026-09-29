// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Namespace-scoped VideoTask access and streaming reads from dedicated result storage.

use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use reqwest::{Client, Method, Response, Url};
use serde::{Deserialize, Serialize};
use tokio::io::AsyncReadExt;
use uuid::Uuid;

const SERVICE_ACCOUNT: &str = "/var/run/secrets/kubernetes.io/serviceaccount";

pub(crate) struct VideoTaskClient {
    client: Client,
    collection: Url,
    output_mount: PathBuf,
    frontend_uid: String,
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

#[derive(Debug, Deserialize)]
pub(crate) struct VideoTaskResource {
    pub metadata: TaskMetadata,
    spec: TaskScope,
    #[serde(default)]
    pub status: VideoTaskStatus,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TaskScope {
    #[serde(rename = "frontendUID")]
    frontend_uid: String,
}

#[derive(Debug, Deserialize)]
pub(crate) struct TaskMetadata {
    pub name: String,
    #[serde(default)]
    labels: BTreeMap<String, String>,
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
        let namespace = std::fs::read_to_string(format!("{SERVICE_ACCOUNT}/namespace"))?;
        let ca = std::fs::read(format!("{SERVICE_ACCOUNT}/ca.crt"))?;
        let client = Client::builder()
            .add_root_certificate(reqwest::Certificate::from_pem(&ca)?)
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()?;
        let mut collection = Url::parse("https://kubernetes.default.svc")?;
        collection
            .path_segments_mut()
            .map_err(|_| "invalid Kubernetes API URL")?
            .extend([
                "apis",
                "inference.foretoken.io",
                "v1alpha1",
                "namespaces",
                namespace.trim(),
                "videotasks",
            ]);
        Ok(Some(Arc::new(Self {
            client,
            collection,
            output_mount: std::fs::canonicalize(output_mount)?,
            frontend_uid: std::env::var("FORETOKEN_VIDEO_TASK_FRONTEND_UID")?,
            worker_image: std::env::var("FORETOKEN_VIDEO_TASK_WORKER_IMAGE")?,
            worker_endpoint: std::env::var("FORETOKEN_VIDEO_TASK_ENDPOINT")?,
            output_claim: std::env::var("FORETOKEN_VIDEO_TASK_OUTPUT_CLAIM")?,
        })))
    }

    /// Persists generation intent and stages inputs, returning the task ID to the API adapter.
    pub(crate) async fn create(
        &self,
        model_service: &str,
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
        let body = serde_json::json!({
            "apiVersion": "inference.foretoken.io/v1alpha1",
            "kind": "VideoTask",
            "metadata": {"name": id, "labels": {"inference.foretoken.io/video-frontend-uid": self.frontend_uid}},
            "spec": {
                "frontendUID": self.frontend_uid,
                "inputsReady": inputs_ready,
                "modelServiceRef": {"name": model_service},
                "request": request,
                "worker": {
                    "image": self.worker_image,
                    "endpoint": self.worker_endpoint,
                    "outputClaimName": self.output_claim,
                    "outputPath": format!("tasks/{id}/result.mp4"),
                },
            },
        });
        self.send(Method::POST, self.collection.clone(), Some(body))
            .await?;
        // The durable task exists before any private file is written. Incomplete staging is
        // observable and expires through the same controller cleanup as finished generation.
        if !inputs_ready {
            let directory = self.output_mount.join(format!("tasks/{id}"));
            tokio::fs::create_dir_all(&directory)
                .await
                .map_err(|_| StatusCode::INSUFFICIENT_STORAGE)?;
            for (source, target) in sources {
                tokio::fs::copy(source, &target)
                    .await
                    .map_err(|_| StatusCode::INSUFFICIENT_STORAGE)?;
                tokio::fs::File::open(target)
                    .await
                    .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
                    .sync_all()
                    .await
                    .map_err(|_| StatusCode::INSUFFICIENT_STORAGE)?;
            }
            tokio::fs::File::open(directory)
                .await
                .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
                .sync_all()
                .await
                .map_err(|_| StatusCode::INSUFFICIENT_STORAGE)?;
            self.send(
                Method::PATCH,
                self.resource_url(&id)?,
                Some(serde_json::json!({"spec":{"inputsReady":true}})),
            )
            .await?;
        }
        Ok(id)
    }

    /// Returns only tasks submitted through this FrontendService, across any of its replicas.
    pub(crate) async fn get(&self, id: &str) -> Result<VideoTaskResource, StatusCode> {
        let task: VideoTaskResource = self
            .send(Method::GET, self.resource_url(id)?, None)
            .await?
            .json()
            .await
            .map_err(|_| StatusCode::BAD_GATEWAY)?;
        if task.spec.frontend_uid != self.frontend_uid
            || task
                .metadata
                .labels
                .get("inference.foretoken.io/video-frontend-uid")
                != Some(&self.frontend_uid)
        {
            return Err(StatusCode::NOT_FOUND);
        }
        Ok(task)
    }

    /// Records cancellation intent; the controller confirms termination before publishing Cancelled.
    pub(crate) async fn cancel(&self, id: &str) -> Result<(), StatusCode> {
        self.get(id).await?;
        self.send(
            Method::PATCH,
            self.resource_url(id)?,
            Some(serde_json::json!({"spec":{"cancelRequested":true}})),
        )
        .await?;
        Ok(())
    }

    /// Requests task deletion and controller-owned artifact cleanup.
    pub(crate) async fn delete(&self, id: &str) -> Result<(), StatusCode> {
        self.get(id).await?;
        self.send(Method::DELETE, self.resource_url(id)?, None)
            .await?;
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
        let mut file = tokio::fs::File::open(path)
            .await
            .map_err(|_| StatusCode::NOT_FOUND)?;
        Ok(Body::from_stream(async_stream::stream! {
            let mut buffer = vec![0u8; 64 * 1024];
            loop {
                match file.read(&mut buffer).await {
                    Ok(0) => break,
                    Ok(count) => yield Ok::<_, std::io::Error>(bytes::Bytes::copy_from_slice(&buffer[..count])),
                    Err(error) => { yield Err(error); break; }
                }
            }
        }))
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
            .map_err(|_| StatusCode::NOT_FOUND)?;
        if !resolved.starts_with(&self.output_mount) {
            return Err(StatusCode::BAD_REQUEST);
        }
        Ok(resolved)
    }

    fn resource_url(&self, id: &str) -> Result<Url, StatusCode> {
        let value = id.strip_prefix("video-").ok_or(StatusCode::NOT_FOUND)?;
        if Uuid::parse_str(value).is_err() {
            return Err(StatusCode::NOT_FOUND);
        }
        let mut url = self.collection.clone();
        url.path_segments_mut()
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
            .push(id);
        Ok(url)
    }

    /// Reloads projected credentials for each API call so token rotation survives long-lived frontends.
    async fn send(
        &self,
        method: Method,
        url: Url,
        body: Option<serde_json::Value>,
    ) -> Result<Response, StatusCode> {
        let token = tokio::fs::read_to_string(format!("{SERVICE_ACCOUNT}/token"))
            .await
            .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
        let patch = method == Method::PATCH;
        let mut request = self.client.request(method, url).bearer_auth(token.trim());
        if patch {
            request = request.header("Content-Type", "application/merge-patch+json");
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().await.map_err(|_| StatusCode::BAD_GATEWAY)?;
        if response.status().is_success() {
            return Ok(response);
        }
        Err(match response.status().as_u16() {
            404 => StatusCode::NOT_FOUND,
            409 => StatusCode::CONFLICT,
            400 | 422 => StatusCode::BAD_REQUEST,
            429 => StatusCode::TOO_MANY_REQUESTS,
            _ => StatusCode::BAD_GATEWAY,
        })
    }
}
