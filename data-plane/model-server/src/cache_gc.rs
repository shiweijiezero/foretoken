// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Collects retired provider snapshots after rechecking consumers under the shared-cache lock.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io;
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;

use foretoken_artifacts::ModelSource;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::Command;
use tokio_util::sync::CancellationToken;

use crate::kubernetes::Kubernetes;
use crate::launch::PYTHON_MODULE_PATH;
use crate::preparation::{
    CACHE_ROOT_KEY, ResolvedArtifacts, lock_model_cache, model_cache_identity, source_publication,
};

const SOURCE_LABEL: &str = "inference.foretoken.io/preparation-pool";
const SOURCE_VOLUME_ANNOTATION: &str = "inference.foretoken.io/model-cache-source-volume";
const SOURCE_CONFIG_MAP_ENV: &str = "FORETOKEN_MODEL_PREPARATION_CONFIGMAP";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CollectionPlan {
    version: u8,
    record: SourceRecord,
    cache: CacheReference,
    volume: SourceVolume,
    collection_volume: CollectionVolume,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceRecord {
    namespace: String,
    name: String,
    uid: String,
}

#[derive(Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CacheReference {
    directory: String,
    claim_name: String,
    #[serde(rename = "claimUID")]
    claim_uid: String,
    mount_path: String,
    directory_owner: Option<Value>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct SourceVolume {
    name: String,
    uid: String,
    claim_ref: Value,
    source: Value,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CollectionVolume {
    name: String,
    uid: String,
    claim_name: String,
    #[serde(rename = "claimUID")]
    claim_uid: String,
}

#[derive(Clone, PartialEq, Eq, Serialize)]
struct CacheItem {
    source: ModelSource,
    repository: String,
    revision: String,
}

/// Mount provenance used to verify the collector alias, independent of claim and directory names.
#[derive(PartialEq)]
enum StorageRoot {
    Nfs { server: String, path: PathBuf },
    HostPath { path: PathBuf, affinity: Value },
}

struct ConsumerSnapshot {
    sources: Vec<Value>,
    namespaces: Vec<Value>,
    pools: Vec<Value>,
    groups: Vec<Value>,
    jobs: Vec<Value>,
    pods: Vec<Value>,
    claims: Vec<Value>,
    volumes: Vec<Value>,
}

impl ConsumerSnapshot {
    /// Reads durable source records before consumers; producers publish under the shared read lock.
    async fn read(client: &Kubernetes) -> io::Result<Self> {
        Ok(Self {
            sources: client
                .list("/api/v1/configmaps", Some(SOURCE_LABEL))
                .await?,
            namespaces: client.list("/api/v1/namespaces", None).await?,
            pools: client
                .list("/apis/inference.foretoken.io/v1alpha1/modelpools", None)
                .await?,
            groups: client
                .list("/apis/inference.foretoken.io/v1alpha1/modelgroups", None)
                .await?,
            jobs: client.list("/apis/batch/v1/jobs", None).await?,
            pods: client.list("/api/v1/pods", None).await?,
            claims: client.list("/api/v1/persistentvolumeclaims", None).await?,
            volumes: client.list("/api/v1/persistentvolumes", None).await?,
        })
    }

    fn namespace_terminating(&self, namespace: &str) -> bool {
        self.namespaces
            .iter()
            .find(|entry| entry["metadata"]["name"] == namespace)
            .is_none_or(deleting)
    }

    /// Verifies the retiring source and both sides of the collector's volume binding.
    /// Its durable origin snapshot retains mount provenance after the original PV disappears.
    fn collection_source<'a>(
        &'a self,
        plan: &CollectionPlan,
        collector: &str,
        identity: &str,
    ) -> io::Result<&'a Value> {
        let record = self
            .sources
            .iter()
            .find(|entry| {
                entry["metadata"]["namespace"] == plan.record.namespace
                    && entry["metadata"]["name"] == plan.record.name
            })
            .ok_or_else(|| invalid("model source ConfigMap is missing"))?;
        validate_record(record, plan)?;
        let alias = self
            .volumes
            .iter()
            .find(|entry| entry["metadata"]["name"] == plan.collection_volume.name)
            .ok_or_else(|| invalid("model cache collection volume is missing"))?;
        validate_collection_resource(alias, plan, collector)?;
        let alias_binding = &alias["spec"]["claimRef"];
        if alias["metadata"]["uid"] != plan.collection_volume.uid
            || deleting(alias)
            || alias_binding["name"] != plan.collection_volume.claim_name
            || alias_binding["uid"] != plan.collection_volume.claim_uid
        {
            return Err(invalid(
                "model cache collection volume no longer matches its claim UID",
            ));
        }
        let namespace = required(alias_binding, "namespace")?;
        if collector.split_once('/').map(|(namespace, _)| namespace) != Some(namespace) {
            return Err(invalid(
                "model cache collection volume belongs to another namespace",
            ));
        }
        let alias_claim = self
            .claims
            .iter()
            .find(|entry| {
                entry["metadata"]["namespace"] == namespace
                    && entry["metadata"]["name"] == plan.collection_volume.claim_name
            })
            .ok_or_else(|| invalid("model cache collection claim is missing"))?;
        validate_collection_resource(alias_claim, plan, collector)?;
        if alias_claim["metadata"]["uid"] != plan.collection_volume.claim_uid
            || alias_claim["spec"]["volumeName"] != plan.collection_volume.name
            || deleting(alias_claim)
        {
            return Err(invalid(
                "model cache collection claim changed its volume binding",
            ));
        }
        let retained: Value = serde_json::from_str(required(
            &alias["metadata"]["annotations"],
            SOURCE_VOLUME_ANNOTATION,
        )?)
        .map_err(io::Error::other)?;
        validate_source_volume(&retained, plan)?;
        // Recreated names are unrelated objects; the retained alias still owns the original dataset.
        let source = match self
            .volumes
            .iter()
            .find(|entry| entry["metadata"]["uid"] == plan.volume.uid)
        {
            Some(source) => {
                validate_source_volume(source, plan)?;
                source
            }
            None => &retained,
        };
        if let Some(claim) = self
            .claims
            .iter()
            .find(|entry| entry["metadata"]["uid"] == plan.cache.claim_uid)
            && (claim["metadata"]["namespace"] != plan.record.namespace
                || claim["metadata"]["name"] != plan.cache.claim_name
                || claim["spec"]["volumeName"] != plan.volume.name)
        {
            return Err(invalid(
                "model cache source claim no longer matches the retiring record",
            ));
        }
        let root = storage_root(source)?;
        if root != storage_root(&retained)?
            || root != storage_root(alias)?
            || alias["spec"]["nodeAffinity"] != source["spec"]["nodeAffinity"]
        {
            return Err(invalid(
                "model cache collection alias mounts a different physical directory",
            ));
        }
        if required(&record["data"], CACHE_ROOT_KEY)? != identity {
            return Err(invalid(
                "model source publication belongs to another cache directory",
            ));
        }
        Ok(record)
    }

    /// Namespace deletion skips controllerless Groups and pause pins, not in-flight producers.
    /// Preparation Jobs and nonterminal Pods must retire before their source can be released.
    fn source_in_use(&self, record: &Value, claim_name: &str) -> io::Result<bool> {
        let namespace = required(&record["metadata"], "namespace")?;
        let name = required(&record["metadata"], "name")?;
        let terminating = self.namespace_terminating(namespace);
        if !terminating {
            for group in &self.groups {
                if group["metadata"]["namespace"] == namespace && group_source_name(group)? == name
                {
                    return Ok(true);
                }
            }
            for pool in &self.pools {
                if pool["metadata"]["namespace"] == namespace
                    && !deleting(pool)
                    && pool["status"]["artifactSource"]["name"] == name
                    && pool["status"]["artifactSource"]["uid"] == record["metadata"]["uid"]
                {
                    return Ok(true);
                }
            }
        }
        for job in &self.jobs {
            if job["metadata"]["namespace"] == namespace
                && !job_terminal(job)
                && pod_uses_source(&job["spec"]["template"]["spec"], name)
            {
                return Ok(true);
            }
        }
        Ok(self.pods.iter().any(|pod| {
            pod["metadata"]["namespace"] == namespace
                && !pod_terminal(pod)
                && (pod_uses_source(&pod["spec"], name)
                    || terminating && pod_uses_claim(&pod["spec"], claim_name))
        }))
    }

    /// Retains fixed publications from this dataset, never unpublished preparation intent.
    /// Producers register under the shared lock before accessing provider files.
    fn live_items(&self, identity: &str) -> io::Result<Vec<CacheItem>> {
        let mut live = Vec::new();
        for record in &self.sources {
            if record["data"][CACHE_ROOT_KEY].as_str() != Some(identity) {
                continue;
            }
            let items = publication_items(record)?;
            if items.is_empty() {
                continue;
            }
            let cache: CacheReference =
                serde_json::from_str(required(&record["data"], "cache.json")?)
                    .map_err(io::Error::other)?;
            if !deleting(record) || self.source_in_use(record, &cache.claim_name)? {
                live.extend(items);
            }
        }
        Ok(live)
    }
}

/// Runs the platform collection Job and returns the provider result for the command's JSON output.
/// Cancellation kills and reaps the deletion child before releasing the exclusive lock.
/// Dropping this future also notifies the child-owning task to complete cleanup.
pub async fn collect() -> io::Result<Value> {
    let plan: CollectionPlan = serde_json::from_str(
        &std::env::var("FORETOKEN_CACHE_COLLECTION").map_err(io::Error::other)?,
    )
    .map_err(io::Error::other)?;
    if plan.version != 1 {
        return Err(invalid("model cache collection plan version must be 1"));
    }
    let root = PathBuf::from(
        std::env::var_os(foretoken_artifacts::MODEL_CACHE_ROOT_ENV).ok_or_else(|| {
            invalid("FORETOKEN_MODEL_CACHE_ROOT must be set for cache collection")
        })?,
    );
    if root != Path::new(&plan.cache.mount_path).join("shared/models") {
        return Err(invalid(
            "model cache root does not match the mounted collection directory",
        ));
    }
    let client = Kubernetes::in_cluster()?;
    let collector = client.service_account_identity()?;
    let cancelled = crate::shutdown_signal();
    tokio::pin!(cancelled);

    // The coordinator completes unpublished sources without a worker. Check publication
    // before opening the cache so a malformed Job cannot initialize an unrelated directory.
    let source_path = format!(
        "/api/v1/namespaces/{}/configmaps/{}",
        plan.record.namespace, plan.record.name
    );
    let current = tokio::select! {
        result = client.get(&source_path) => result?,
        result = &mut cancelled => return Err(cancelled_error(result)),
    };
    validate_record(&current, &plan)?;
    required(&current["data"], CACHE_ROOT_KEY)?;
    if publication_items(&current)?.is_empty() {
        return Err(invalid(
            "model cache collection requires a published remote source",
        ));
    }
    let lock = tokio::select! {
        result = lock_model_cache(&root, true) => result?,
        result = &mut cancelled => return Err(cancelled_error(result)),
    };
    let identity = model_cache_identity(&lock)?;
    let snapshot = tokio::select! {
        result = ConsumerSnapshot::read(&client) => result?,
        result = &mut cancelled => return Err(cancelled_error(result)),
    };
    let record = snapshot.collection_source(&plan, &collector, &identity)?;
    if snapshot.source_in_use(record, &plan.cache.claim_name)? {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "retiring model source still has consumers",
        ));
    }
    let candidates = publication_items(record)?;
    let live = snapshot.live_items(&identity)?;
    let request = json!({"root": root, "candidates": candidates, "live": live});

    // One task owns both the child and lock. Dropping this future cannot release the
    // lock while a detached Python process continues deleting files.
    let cancellation = CancellationToken::new();
    let _cancel_on_drop = cancellation.clone().drop_guard();
    let stopped = cancellation.clone();
    let mut worker = tokio::spawn(async move { run_provider(request, lock, stopped).await });
    tokio::select! {
        biased;
        result = &mut worker => result.map_err(io::Error::other)?,
        result = &mut cancelled => {
            cancellation.cancel();
            worker.await.map_err(io::Error::other)??;
            Err(cancelled_error(result))
        }
    }
}

/// Runs the image's provider adapter while retaining the lock through every child exit path.
async fn run_provider(
    request: Value,
    _lock: File,
    cancellation: CancellationToken,
) -> io::Result<Value> {
    let directory = std::env::var_os(foretoken_artifacts::source::ACTIVE_DIRECTORY_ENV)
        .map(|directory| PathBuf::from(directory).join("python"))
        .unwrap_or_else(|| PathBuf::from(PYTHON_MODULE_PATH));
    let script = fs::read_to_string(directory.join("foretoken_cache_gc.py"))?;
    let payload = serde_json::to_vec(&request).map_err(io::Error::other)?;
    let mut child = Command::new(crate::launch::python_executable())
        .args(["-c", &script])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()?;
    let mut stdin = child.stdin.take().expect("provider stdin was piped");
    let mut stdout = child.stdout.take().expect("provider stdout was piped");
    let result = tokio::select! {
        result = async {
            stdin.write_all(&payload).await?;
            stdin.shutdown().await?;
            drop(stdin);
            let mut output = Vec::new();
            let (status, _) = tokio::try_join!(child.wait(), stdout.read_to_end(&mut output))?;
            if !status.success() {
                return Err(io::Error::other(format!("model cache collection failed with status {status}")));
            }
            serde_json::from_slice(&output).map_err(io::Error::other)
        } => result,
        () = cancellation.cancelled() => Err(io::Error::new(io::ErrorKind::Interrupted, "model cache collection cancelled")),
    };
    if result.is_err() && child.try_wait()?.is_none() {
        child.kill().await?;
    }
    result
}

/// Accepts only collection resources owned by this source UID and worker identity.
fn validate_collection_resource(
    object: &Value,
    plan: &CollectionPlan,
    collector: &str,
) -> io::Result<()> {
    let metadata = &object["metadata"];
    let annotations = &metadata["annotations"];
    if metadata["labels"]["inference.foretoken.io/model-cache-collection"] != plan.record.uid
        || annotations["inference.foretoken.io/model-cache-source-namespace"]
            != plan.record.namespace
        || annotations["inference.foretoken.io/model-cache-source-name"] != plan.record.name
        || annotations["inference.foretoken.io/model-cache-collector"] != collector
    {
        return Err(invalid(
            "model cache collection resource belongs to another source or collector",
        ));
    }
    Ok(())
}

/// Verifies live and retained PV origins without adopting replacement volumes or claims.
fn validate_source_volume(volume: &Value, plan: &CollectionPlan) -> io::Result<()> {
    if volume["metadata"]["name"] != plan.volume.name
        || volume["metadata"]["uid"] != plan.volume.uid
        || volume["spec"]["claimRef"] != plan.volume.claim_ref
        || volume["spec"]["claimRef"]["namespace"] != plan.record.namespace
        || volume["spec"]["claimRef"]["name"] != plan.cache.claim_name
        || volume["spec"]["claimRef"]["uid"] != plan.cache.claim_uid
        || !source_matches(&volume["spec"], &plan.volume.source)?
    {
        return Err(invalid(
            "model cache source volume changed its UID, source or claim binding",
        ));
    }
    Ok(())
}

/// Binds each execution to its source ConfigMap UID and controller-recorded storage selection.
fn validate_record(record: &Value, plan: &CollectionPlan) -> io::Result<()> {
    if record["metadata"]["namespace"] != plan.record.namespace
        || record["metadata"]["name"] != plan.record.name
        || record["metadata"]["uid"] != plan.record.uid
        || !deleting(record)
    {
        return Err(invalid(
            "model source ConfigMap is not the retiring record UID",
        ));
    }
    let cache: CacheReference =
        serde_json::from_str(required(&record["data"], "cache.json")?).map_err(io::Error::other)?;
    if cache != plan.cache {
        return Err(invalid(
            "model source cache binding changed after collection was submitted",
        ));
    }
    Ok(())
}

fn cancelled_error(result: io::Result<()>) -> io::Error {
    result.err().unwrap_or_else(|| {
        io::Error::new(
            io::ErrorKind::Interrupted,
            "model cache collection cancelled",
        )
    })
}

fn publication_items(record: &Value) -> io::Result<Vec<CacheItem>> {
    let Some(encoded) = record["data"]["source.json"]
        .as_str()
        .filter(|record| !record.is_empty())
    else {
        return Ok(Vec::new());
    };
    Ok(source_items(source_publication(encoded.as_bytes())?))
}

fn source_items(source: ResolvedArtifacts) -> Vec<CacheItem> {
    if source.artifacts.source == ModelSource::Local {
        return Vec::new();
    }
    vec![
        CacheItem {
            source: source.artifacts.source,
            repository: source.artifacts.model,
            revision: source.model_revision,
        },
        CacheItem {
            source: source.artifacts.source,
            repository: source.artifacts.tokenizer,
            revision: source.tokenizer_revision,
        },
    ]
}

fn group_source_name(group: &Value) -> io::Result<String> {
    Ok(format!(
        "model-source-{}-{}",
        required(&group["spec"]["modelPoolRef"], "uid")?,
        required(&group["spec"], "revision")?,
    ))
}

fn pod_uses_source(spec: &Value, name: &str) -> bool {
    array(&spec["volumes"]).iter().any(|volume| {
        volume["configMap"]["name"] == name
            || array(&volume["projected"]["sources"])
                .iter()
                .any(|source| source["configMap"]["name"] == name)
    }) || containers(spec).any(|container| {
        array(&container["env"])
            .iter()
            .any(|variable| variable["name"] == SOURCE_CONFIG_MAP_ENV && variable["value"] == name)
    })
}

fn pod_uses_claim(spec: &Value, claim: &str) -> bool {
    array(&spec["volumes"])
        .iter()
        .any(|volume| volume["persistentVolumeClaim"]["claimName"] == claim)
}

fn containers(spec: &Value) -> impl Iterator<Item = &Value> {
    array(&spec["initContainers"])
        .iter()
        .chain(array(&spec["containers"]))
}

fn pod_terminal(pod: &Value) -> bool {
    matches!(
        pod["status"]["phase"].as_str(),
        Some("Succeeded" | "Failed")
    )
}

fn job_terminal(job: &Value) -> bool {
    array(&job["status"]["conditions"]).iter().any(|condition| {
        condition["status"] == "True"
            && matches!(condition["type"].as_str(), Some("Complete" | "Failed"))
    })
}

fn deleting(object: &Value) -> bool {
    object["metadata"]["deletionTimestamp"].is_string()
}

/// Resolves supported filesystem provenance to verify the collector's actual mount.
/// NFS CSI mounts its volume attributes; volumeHandle is not a directory identity.
fn storage_root(volume: &Value) -> io::Result<StorageRoot> {
    let spec = &volume["spec"];
    if let Some(nfs) = spec.get("nfs") {
        return Ok(StorageRoot::Nfs {
            server: nfs_server(required(nfs, "server")?),
            path: storage_path(required(nfs, "path")?)?,
        });
    }
    if let Some(host) = spec.get("hostPath") {
        return Ok(StorageRoot::HostPath {
            path: storage_path(required(host, "path")?)?,
            affinity: spec["nodeAffinity"].clone(),
        });
    }
    let csi = &spec["csi"];
    if csi["driver"] != "nfs.csi.k8s.io" {
        return Err(invalid(
            "model cache collection requires native NFS, hostPath or nfs.csi.k8s.io storage",
        ));
    }
    let mut attributes = BTreeMap::new();
    for (key, value) in csi["volumeAttributes"]
        .as_object()
        .ok_or_else(|| invalid("NFS CSI volume has no volume attributes"))?
    {
        let key = key.to_lowercase();
        if !matches!(
            key.as_str(),
            "server"
                | "share"
                | "subdir"
                | "mountoptions"
                | "csi.storage.k8s.io/pvc/namespace"
                | "csi.storage.k8s.io/pvc/name"
                | "csi.storage.k8s.io/pv/name"
        ) {
            continue;
        }
        let value = value
            .as_str()
            .ok_or_else(|| invalid("NFS CSI volume attribute is not a string"))?;
        if let Some(previous) = attributes.insert(key, value.to_owned())
            && previous != value
        {
            return Err(invalid(
                "NFS CSI volume has conflicting case-insensitive attributes",
            ));
        }
    }
    let server = attributes
        .get("server")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| invalid("NFS CSI volume has no server attribute"))?;
    let share = attributes
        .get("share")
        .ok_or_else(|| invalid("NFS CSI volume has no share attribute"))?;
    let mut subdirectory = attributes.get("subdir").cloned().unwrap_or_default();
    for (placeholder, key) in [
        (
            "${pvc.metadata.namespace}",
            "csi.storage.k8s.io/pvc/namespace",
        ),
        ("${pvc.metadata.name}", "csi.storage.k8s.io/pvc/name"),
        ("${pv.metadata.name}", "csi.storage.k8s.io/pv/name"),
    ] {
        if let Some(value) = attributes.get(key) {
            subdirectory = subdirectory.replace(placeholder, value);
        }
    }
    let path = if subdirectory.is_empty() {
        share.clone()
    } else {
        format!("{}/{subdirectory}", share.trim_end_matches('/'))
    };
    Ok(StorageRoot::Nfs {
        server: nfs_server(server),
        path: storage_path(&path)?,
    })
}

fn nfs_server(value: &str) -> String {
    value.parse::<std::net::IpAddr>().map_or_else(
        |_| value.trim_end_matches('.').to_ascii_lowercase(),
        |address| address.to_string(),
    )
}

fn storage_path(value: &str) -> io::Result<PathBuf> {
    let path = Path::new(value);
    if !path.is_absolute()
        || value.contains(['\\', '\0'])
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
    {
        return Err(invalid(
            "model cache volume requires an absolute storage path without parent traversal",
        ));
    }
    Ok(path.components().collect())
}

fn source_matches(spec: &Value, source: &Value) -> io::Result<bool> {
    let source = source
        .as_object()
        .ok_or_else(|| invalid("model cache plan has no volume source"))?;
    if source.is_empty() {
        return Err(invalid("model cache plan has an empty volume source"));
    }
    Ok(source
        .iter()
        .all(|(key, value)| spec.get(key) == Some(value)))
}

fn array(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn required<'a>(object: &'a Value, key: &str) -> io::Result<&'a str> {
    object[key]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| invalid(format!("model cache object requires {key}")))
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}
