// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Kubernetes requests shared by source publication and cache collection workers.

use std::fs;
use std::io;
use std::path::PathBuf;

use axum::http::{Request, header};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use kube::api::{ListParams, ObjectList};
use kube::{Client, Config};
use serde_json::Value;

/// Uses the worker's projected account through kube-rs; callers own publication retries.
pub(crate) struct Kubernetes {
    client: Client,
    token: PathBuf,
}

impl Kubernetes {
    /// Loads only in-cluster configuration; kube-rs owns TLS and projected-token rotation.
    pub(crate) fn in_cluster() -> io::Result<Self> {
        let mut config = Config::incluster().map_err(io::Error::other)?;
        config.default_retry = false;
        let token = config
            .auth_info
            .token_file
            .as_ref()
            .map(PathBuf::from)
            .ok_or_else(|| io::Error::other("in-cluster configuration has no projected token"))?;
        Ok(Self {
            client: Client::try_from(config).map_err(io::Error::other)?,
            token,
        })
    }

    /// Reads the projected account identity used to match collection-resource ownership.
    /// The API server authenticates requests made by the same Pod service account.
    pub(crate) fn service_account_identity(&self) -> io::Result<String> {
        let token = fs::read_to_string(&self.token)?;
        let claims = token.split('.').nth(1).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "projected service account token has no claims",
            )
        })?;
        let claims: Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(claims).map_err(io::Error::other)?)
                .map_err(io::Error::other)?;
        let account = claims["sub"]
            .as_str()
            .and_then(|subject| subject.strip_prefix("system:serviceaccount:"))
            .and_then(|account| account.split_once(':'))
            .filter(|(namespace, name)| {
                !namespace.is_empty() && !name.is_empty() && !name.contains(':')
            })
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "projected token has no service account identity",
                )
            })?;
        Ok(format!("{}/{}", account.0, account.1))
    }

    /// Reads an API object directly rather than relying on a controller's informer snapshot.
    pub(crate) async fn get(&self, path: &str) -> io::Result<Value> {
        let request = Request::get(path)
            .body(Vec::new())
            .map_err(io::Error::other)?;
        self.client.request(request).await.map_err(io::Error::other)
    }

    /// Sends a merge patch while leaving conflict handling with the publishing owner.
    pub(crate) async fn patch(&self, path: &str, value: &Value) -> Result<Value, kube::Error> {
        let request = Request::patch(path)
            .header(header::CONTENT_TYPE, "application/merge-patch+json")
            .body(serde_json::to_vec(value).map_err(kube::Error::SerdeError)?)
            .map_err(kube::Error::HttpError)?;
        self.client.request(request).await
    }

    /// Reads every returned page so omitted resources cannot be mistaken for released cache references.
    pub(crate) async fn list(&self, path: &str, selector: Option<&str>) -> io::Result<Vec<Value>> {
        let requests = kube::core::Request::new(path);
        let mut parameters = ListParams {
            label_selector: selector.map(str::to_owned),
            ..Default::default()
        };
        let mut items = Vec::new();
        loop {
            let request = requests.list(&parameters).map_err(io::Error::other)?;
            let page: ObjectList<Value> = self
                .client
                .request(request)
                .await
                .map_err(io::Error::other)?;
            items.extend(page.items);
            parameters.continue_token = page.metadata.continue_.filter(|value| !value.is_empty());
            if parameters.continue_token.is_none() {
                return Ok(items);
            }
        }
    }
}
