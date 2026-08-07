use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use foretoken_backend_registry::BackendRegistry;
use foretoken_router::{NeutralScorer, PolicyRouter, RouterAlgorithm};
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuilder};
use foretoken_server::{Generation, RuntimeControl, RuntimeGeneration, RuntimeState};

use super::{
    install_serving_snapshot, refresh_active_generation, snapshot_is_current,
    watch_serving_snapshot,
};

fn runtime_builder() -> RuntimeBuilder {
    RuntimeBuilder::new(RouterAlgorithm::KvAware, KvIndexCredential::Disabled)
}

struct ReadyControl;

#[async_trait]
impl RuntimeControl for ReadyControl {
    async fn refresh_backend_readiness(&self) {}

    fn healthy_models(&self) -> Vec<String> {
        vec!["active".into()]
    }

    fn is_ready(&self) -> bool {
        true
    }
}

struct CountingControl {
    refreshes: Arc<AtomicUsize>,
}

#[async_trait]
impl RuntimeControl for CountingControl {
    async fn refresh_backend_readiness(&self) {
        self.refreshes.fetch_add(1, Ordering::Relaxed);
    }

    fn healthy_models(&self) -> Vec<String> {
        vec!["active".into()]
    }

    fn is_ready(&self) -> bool {
        true
    }
}

fn empty_state() -> Arc<RuntimeState> {
    let registry = Arc::new(
        BackendRegistry::from_json(
            br#"{"version":1,"groups":[{"backend_id":"active","model":"active","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1"}]}"#,
        )
        .unwrap(),
    );
    Arc::new(RuntimeState::new(
        Default::default(),
        Arc::new(PolicyRouter::new(registry.clone(), Arc::new(NeutralScorer))),
        registry,
    ))
}

fn active_generation(version: u64) -> Arc<RuntimeGeneration> {
    let generation = Arc::new(RuntimeGeneration::new());
    assert!(generation.replace_state(version, empty_state(), Arc::new(ReadyControl)));
    generation
}

#[tokio::test]
async fn invalid_or_unready_candidate_keeps_the_active_generation() {
    let generation = active_generation(10);

    assert!(!install_serving_snapshot(&generation, b"not-json", &runtime_builder()).await);
    assert!(generation.ready());
    assert_eq!(generation.active_version(), Some(10));

    let unready = br#"{"version":11,"groups":[{"backend_id":"candidate","model":"candidate","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1"}]}"#;
    assert!(!install_serving_snapshot(&generation, unready, &runtime_builder()).await);
    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["active"]);
    assert_eq!(generation.active_version(), Some(10));
}

#[tokio::test]
async fn initial_failure_stays_unavailable_and_stale_candidate_is_ignored() {
    let initial = RuntimeGeneration::new();
    assert!(!install_serving_snapshot(&initial, b"not-json", &runtime_builder()).await);
    assert!(!initial.ready());
    assert_eq!(initial.active_version(), None);

    let generation = active_generation(10);
    let stale = br#"{"version":9,"groups":[{"backend_id":"stale","model":"stale","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1"}]}"#;
    assert!(!install_serving_snapshot(&generation, stale, &runtime_builder()).await);
    assert!(generation.ready());
    assert_eq!(generation.active_version(), Some(10));
}

#[tokio::test]
async fn active_readiness_refresh_runs_independently_from_candidate_watching() {
    let refreshes = Arc::new(AtomicUsize::new(0));
    let generation = Arc::new(RuntimeGeneration::new());
    assert!(generation.replace_state(
        10,
        empty_state(),
        Arc::new(CountingControl {
            refreshes: refreshes.clone(),
        }),
    ));
    let refresher = tokio::spawn(refresh_active_generation(generation));
    tokio::time::sleep(std::time::Duration::from_millis(1_100)).await;
    refresher.abort();

    assert!(refreshes.load(Ordering::Relaxed) >= 1);
}

#[tokio::test]
async fn unreadable_snapshot_file_never_revokes_the_active_generation() {
    let generation = active_generation(10);
    let path = std::env::temp_dir().join(format!(
        "foretoken-missing-serving-snapshot-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    let watcher = tokio::spawn(watch_serving_snapshot(
        generation.clone(),
        path,
        Some(b"installed".to_vec()),
        Arc::new(runtime_builder()),
    ));
    tokio::time::sleep(std::time::Duration::from_millis(1_100)).await;
    watcher.abort();

    assert!(generation.ready());
    assert_eq!(generation.models(), vec!["active"]);
    assert_eq!(generation.active_version(), Some(10));
}

#[test]
fn candidate_publication_requires_the_same_snapshot_bytes_on_disk() {
    let path = std::env::temp_dir().join(format!(
        "foretoken-latest-serving-snapshot-{}",
        std::process::id()
    ));
    std::fs::write(&path, b"version-12").unwrap();
    assert!(!snapshot_is_current(&path, b"version-11").unwrap());
    assert!(snapshot_is_current(&path, b"version-12").unwrap());
    std::fs::remove_file(path).unwrap();
}
