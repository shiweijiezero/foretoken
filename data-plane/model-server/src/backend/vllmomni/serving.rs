// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns Omni startup, HTTP supervision, and the accepted-request drain before native shutdown.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;

use super::api::{self, AppState};
use super::launch::LaunchPlan;
use crate::engine::{ManagedEngine, python_executable};
use crate::execution::RuntimeHealth;
use crate::shutdown_signal;

pub(super) struct Server {
    server: tokio::task::JoinHandle<std::io::Result<()>>,
    child: ManagedEngine,
    health: Arc<RuntimeHealth>,
    shutdown: CancellationToken,
    drain: Duration,
}

enum Stop {
    Signal,
    Child(std::process::ExitStatus),
    Server,
    Error(Box<dyn std::error::Error>),
}

impl Server {
    /// Prepares fixed model files and a healthy native process before opening video intake.
    pub(super) async fn start() -> Result<Self, Box<dyn std::error::Error>> {
        let (plan, listen) = LaunchPlan::from_env()?;
        let upstream = format!("http://127.0.0.1:{}", plan.upstream_port);
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .build()?;
        let listener = TcpListener::bind(listen).await?;
        let deadline = Instant::now() + Duration::from_secs(plan.lifecycle.startup_seconds);
        let model = plan.prepare_model(&python_executable(), deadline).await?;
        let child = ManagedEngine::spawn(plan.engine_command(&model)?, None).await?;
        if let Err(error) = wait_until_ready(&client, &upstream, &child, deadline).await {
            child.shutdown(Duration::from_secs(10)).await?;
            return Err(error);
        }

        // Intake starts only after native readiness; the returned owner retains both serving tasks.
        let health = Arc::new(RuntimeHealth::new());
        health.set_process_alive(true);
        health.set_client_healthy(true);
        health.set_accepting(true);
        let app = api::router(AppState {
            client,
            upstream,
            model: plan.artifacts.model,
            revision: plan.artifacts.revision,
            health: health.clone(),
        });
        let shutdown = CancellationToken::new();
        let server_shutdown = shutdown.clone();
        let server = tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(server_shutdown.cancelled_owned())
                .await
        });
        eprintln!("Foretoken vLLM-Omni adapter ready on {listen}");
        Ok(Self {
            server,
            child,
            health,
            shutdown,
            drain: Duration::from_secs(plan.lifecycle.drain_seconds),
        })
    }

    /// Supervises the HTTP/native pair and retires it through one admission-close and drain path.
    pub(super) async fn serve(mut self) -> Result<(), Box<dyn std::error::Error>> {
        let stop = self.wait_for_stop().await;
        let reason = match &stop {
            Stop::Signal => "shutdown signal".to_string(),
            Stop::Child(status) => format!("vLLM-Omni exited: {status}"),
            Stop::Server => "HTTP server stopped".to_string(),
            Stop::Error(error) => format!("adapter error: {error}"),
        };
        eprintln!("Stopping Foretoken vLLM-Omni adapter: {reason}");
        self.drain(matches!(&stop, Stop::Signal)).await?;
        match stop {
            Stop::Error(error) => Err(error),
            _ => Ok(()),
        }
    }

    /// Observes termination without transferring child or server ownership out of this supervisor.
    async fn wait_for_stop(&mut self) -> Stop {
        tokio::select! {
            signal = shutdown_signal() => match signal {
                Ok(()) => Stop::Signal,
                Err(error) => Stop::Error(Box::new(error)),
            },
            status = self.child.wait_for_exit() => Stop::Child(status),
            result = &mut self.server => match result {
                Ok(Ok(())) => Stop::Server,
                Ok(Err(error)) => Stop::Error(Box::new(error)),
                Err(error) => Stop::Error(Box::new(error)),
            },
        }
    }

    /// Closes new intake and retains accepted HTTP streams before stopping their native process.
    async fn drain(&mut self, graceful: bool) -> Result<(), Box<dyn std::error::Error>> {
        self.health.set_accepting(false);
        if graceful
            && tokio::time::timeout(self.drain, self.health.wait_drained())
                .await
                .is_err()
        {
            eprintln!("Accepted requests did not finish before the drain deadline");
        }
        self.health.set_process_alive(false);
        self.shutdown.cancel();
        if !self.server.is_finished()
            && tokio::time::timeout(Duration::from_secs(5), &mut self.server)
                .await
                .is_err()
        {
            self.server.abort();
            let _ = (&mut self.server).await;
        }
        self.child.shutdown(Duration::from_secs(10)).await?;
        Ok(())
    }
}

/// Observes native health and child exit within the same model-preparation and startup budget.
async fn wait_until_ready(
    client: &reqwest::Client,
    upstream: &str,
    child: &ManagedEngine,
    deadline: Instant,
) -> Result<(), Box<dyn std::error::Error>> {
    loop {
        if let Some(status) = child.try_wait().await {
            return Err(format!("vLLM-Omni exited during startup: {status}").into());
        }
        if matches!(
            client
                .get(format!("{upstream}/health"))
                .timeout(Duration::from_secs(1))
                .send()
                .await,
            Ok(response) if response.status().is_success()
        ) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("vLLM-Omni startup deadline elapsed".into());
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}
