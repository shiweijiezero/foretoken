// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Kubernetes requests shared by source publication and cache collection workers.

use std::fs;
use std::io;
use std::path::PathBuf;

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use reqwest::{Client, Method, Response, Url};
use serde_json::Value;

/// Uses the worker's projected account, reloading its token for every API request.
pub(crate) struct Kubernetes {
    client: Client,
    endpoint: Url,
    token: PathBuf,
}

impl Kubernetes {
    /// Loads the in-cluster endpoint and certificate without forwarding credentials through proxies.
    pub(crate) fn in_cluster() -> io::Result<Self> {
        let host = std::env::var("KUBERNETES_SERVICE_HOST").map_err(io::Error::other)?;
        let port = std::env::var("KUBERNETES_SERVICE_PORT_HTTPS").map_err(io::Error::other)?;
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host
        };
        let account = PathBuf::from("/var/run/secrets/kubernetes.io/serviceaccount");
        let client = Client::builder()
            .no_proxy()
            .add_root_certificate(
                reqwest::Certificate::from_pem(&fs::read(account.join("ca.crt"))?)
                    .map_err(io::Error::other)?,
            )
            .build()
            .map_err(io::Error::other)?;
        Ok(Self {
            client,
            endpoint: Url::parse(&format!("https://{host}:{port}")).map_err(io::Error::other)?,
            token: account.join("token"),
        })
    }

    /// Reads the projected account identity used to match collection-resource ownership.
    /// The API server authenticates requests made with this same token.
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

    fn request(&self, method: Method, path: &str) -> io::Result<reqwest::RequestBuilder> {
        let mut url = self.endpoint.clone();
        url.set_path(path);
        Ok(self
            .client
            .request(method, url)
            .bearer_auth(fs::read_to_string(&self.token)?.trim()))
    }

    /// Reads an API object directly rather than relying on a controller's informer snapshot.
    pub(crate) async fn get(&self, path: &str) -> io::Result<Value> {
        self.request(Method::GET, path)?
            .send()
            .await
            .map_err(io::Error::other)?
            .error_for_status()
            .map_err(io::Error::other)?
            .json()
            .await
            .map_err(io::Error::other)
    }

    /// Sends a merge patch while leaving conflict handling with the publishing owner.
    pub(crate) async fn patch(&self, path: &str, value: &Value) -> io::Result<Response> {
        self.request(Method::PATCH, path)?
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/merge-patch+json",
            )
            .json(value)
            .send()
            .await
            .map_err(io::Error::other)
    }

    /// Reads every returned page so omitted resources cannot be mistaken for released cache references.
    pub(crate) async fn list(&self, path: &str, selector: Option<&str>) -> io::Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut continuation = String::new();
        loop {
            let mut request = self.request(Method::GET, path)?;
            if let Some(selector) = selector {
                request = request.query(&[("labelSelector", selector)]);
            }
            if !continuation.is_empty() {
                request = request.query(&[("continue", &continuation)]);
            }
            let page: Value = request
                .send()
                .await
                .map_err(io::Error::other)?
                .error_for_status()
                .map_err(io::Error::other)?
                .json()
                .await
                .map_err(io::Error::other)?;
            items.extend(
                page["items"]
                    .as_array()
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "Kubernetes list response has no items",
                        )
                    })?
                    .iter()
                    .cloned(),
            );
            continuation = page["metadata"]["continue"]
                .as_str()
                .unwrap_or_default()
                .into();
            if continuation.is_empty() {
                return Ok(items);
            }
        }
    }
}
