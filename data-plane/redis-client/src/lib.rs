// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reconnecting Redis connections and keyed wakeups for independently owned state stores.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures::FutureExt;
use futures::future::{BoxFuture, Shared};
use redis::aio::{ConnectionManager, ConnectionManagerConfig};
use redis::{ConnectionInfo, Msg, ProtocolVersion, PushInfo, PushKind, RedisResult};
use tokio::sync::{OnceCell, watch};

type Watchers = Arc<Mutex<HashMap<String, watch::Sender<()>>>>;
type Subscription = Shared<BoxFuture<'static, Option<ConnectionManager>>>;

/// Owns one store's lazy data connection and optional native subscription, without interpreting records.
pub struct RedisConnection {
    client: redis::Client,
    manager: OnceCell<ConnectionManager>,
    subscription: Subscription,
    channel: String,
    watchers: Watchers,
}

/// One active observer's wakeup registration; dropping it removes the last observer for its key.
pub struct KeyWatch {
    key: String,
    receiver: Option<watch::Receiver<()>>,
    watchers: Watchers,
    subscription: Subscription,
}

impl RedisConnection {
    /// Validates one endpoint and chooses a database-scoped channel for the owning store's domain.
    pub fn new(url: &str, domain: &str) -> RedisResult<Self> {
        let client = redis::Client::open(url)?;
        // Pub/Sub shares a server-wide namespace, unlike the stored keys.
        let channel = format!(
            "{domain}:changes:db:{}",
            client.get_connection_info().redis.db
        );
        let watchers = Arc::new(Mutex::new(HashMap::new()));
        let subscription = Self::create_subscription(
            client.get_connection_info().clone(),
            channel.clone(),
            watchers.clone(),
        )
        .map(|result| match result {
            Ok(subscription) => Some(subscription),
            Err(_) => {
                tracing::warn!(
                    "Redis change subscription unavailable; this client will poll retained state"
                );
                None
            }
        })
        .boxed()
        .shared();
        Ok(Self {
            client,
            manager: OnceCell::new(),
            subscription,
            channel,
            watchers,
        })
    }

    /// Returns upstream connection identity for domain owners that distinguish independent stores.
    pub fn connection_info(&self) -> &ConnectionInfo {
        self.client.get_connection_info()
    }

    /// Returns the channel passed to the owning store's atomic write script.
    pub fn channel(&self) -> &str {
        &self.channel
    }

    /// Shares a native multiplexed data connection, retaining the endpoint's configured protocol.
    pub async fn get(&self) -> RedisResult<ConnectionManager> {
        let manager = self
            .manager
            .get_or_try_init(|| {
                self.client.get_connection_manager_with_config(
                    ConnectionManagerConfig::new()
                        .set_connection_timeout(Duration::from_secs(5))
                        .set_max_delay(1_000),
                )
            })
            .await?;
        Ok(manager.clone())
    }

    /// Registers before the caller reads state, without waiting for the optional notification transport.
    pub fn watch(&self, key: String) -> KeyWatch {
        let receiver = self
            .watchers
            .lock()
            .expect("Redis watcher lock poisoned")
            .entry(key.clone())
            .or_insert_with(|| watch::channel(()).0)
            .subscribe();
        KeyWatch {
            key,
            receiver: Some(receiver),
            watchers: self.watchers.clone(),
            subscription: self.subscription.clone(),
        }
    }

    /// Starts the advisory transport once; successful subscriptions reconnect through redis-rs.
    async fn create_subscription(
        mut info: ConnectionInfo,
        channel: String,
        watchers: Watchers,
    ) -> RedisResult<ConnectionManager> {
        // A restricted endpoint may permit data commands without RESP3 or Pub/Sub.
        // Keep subscription transport separate so advisory wakeups cannot disable state access.
        info.redis.protocol = ProtocolVersion::RESP3;
        let mut subscription = redis::Client::open(info)?
            .get_connection_manager_with_config(
                ConnectionManagerConfig::new()
                    .set_connection_timeout(Duration::from_secs(5))
                    .set_max_delay(1_000)
                    .set_automatic_resubscription()
                    .set_push_sender(move |push: PushInfo| -> Result<(), redis::aio::SendError> {
                        let watchers = watchers.lock().expect("Redis watcher lock poisoned");
                        if matches!(push.kind, PushKind::Disconnection | PushKind::Subscribe) {
                            for changed in watchers.values() {
                                changed.send_modify(|_| {});
                            }
                        } else if let Some(message) = Msg::from_push_info(push)
                            && let Ok(key) = message.get_payload::<String>()
                            && let Some(changed) = watchers.get(&key)
                        {
                            changed.send_modify(|_| {});
                        }
                        Ok(())
                    }),
            )
            .await?;
        subscription.subscribe(&channel).await?;
        Ok(subscription)
    }
}

impl KeyWatch {
    /// Waits for a change or reconciles older writers and messages missed during reconnect.
    /// A wakeup never establishes completion or authorizes access; callers must reread their own store.
    pub async fn changed(&mut self) {
        // Poll clones so cancellation and periodic reads preserve the shared connection attempt.
        // The retained handle observes completion without repeatedly selecting a ready future.
        tokio::select! {
            _ = self.receiver.as_mut().expect("active Redis watcher").changed() => {},
            _ = self.subscription.clone(), if self.subscription.peek().is_none() => {},
            _ = tokio::time::sleep(Duration::from_secs(1)) => {},
        }
    }
}

impl Drop for KeyWatch {
    fn drop(&mut self) {
        let mut watchers = self.watchers.lock().expect("Redis watcher lock poisoned");
        // Serialize receiver release with registration and other drops so two last
        // observers cannot both leave an unobserved sender behind.
        drop(self.receiver.take());
        if watchers
            .get(&self.key)
            .is_some_and(|sender| sender.receiver_count() == 0)
        {
            watchers.remove(&self.key);
        }
    }
}
