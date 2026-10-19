// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared, volatile API history and response events, independent of inference execution state.

mod completions;
mod conversations;
mod records;

use std::sync::{Arc, LazyLock};
use std::time::Duration;

use redis::aio::{ConnectionManager, ConnectionManagerConfig};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::sync::OnceCell;
use uuid::Uuid;

pub(crate) use records::{NewResponse, ResponseRef};

static TRANSITION: LazyLock<redis::Script> =
    LazyLock::new(|| redis::Script::new(include_str!("transition.lua")));

/// Storage failures stay separate from model execution and never expose connections or credentials.
#[derive(Debug, Error)]
pub(crate) enum StoreError {
    #[error("API state storage is not configured")]
    Disabled,
    #[error("invalid API state storage configuration")]
    Configuration,
    #[error("API state storage is unavailable")]
    Storage(#[from] redis::RedisError),
    #[error("the requested object was not found")]
    NotFound,
    #[error("{0}")]
    Invalid(String),
    #[error("the conversation has an active response")]
    ConversationBusy,
    #[error("the response has not finished")]
    ResponseBusy,
    #[error("the response stream has expired")]
    StreamExpired,
    #[error("API state changed during the update")]
    Changed,
    #[error("stored API state is invalid")]
    Corrupt,
}

/// One service's shared cache connection and process registration, independent of model snapshots.
pub(crate) struct ApiStore {
    client: redis::Client,
    connection: OnceCell<ConnectionManager>,
    prefix: String,
    completion_prefix: String,
    ledger: Option<String>,
    pod: String,
    instance: String,
    execution_scope: String,
    retention_seconds: i64,
    replay_seconds: i64,
}

/// The ingress-resolved caller associated with a cached API object.
pub(crate) type Caller = Option<String>;

/// Opaque JSON plus occurrence identities lets Lua resolve references without rewriting public data.
#[derive(Serialize)]
struct CachedItem {
    id: String,
    copy_id: String,
    body: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Order {
    Asc,
    Desc,
}

/// Cursor parameters shared by stored-object and item listings.
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PageQuery {
    pub after: Option<String>,
    pub before: Option<String>,
    pub limit: Option<usize>,
    pub order: Option<Order>,
}

impl ApiStore {
    /// Opens the controller-projected connection lazily; stateless requests need no API cache.
    pub(crate) fn from_env(
        ledger: Option<&foretoken_request_ledger::RequestLedger>,
    ) -> Result<Option<Arc<Self>>, StoreError> {
        let url = match std::env::var("FORETOKEN_RESPONSE_STORE_URL") {
            Ok(value) if !value.is_empty() => value,
            Err(std::env::VarError::NotPresent) => return Ok(None),
            _ => return Err(StoreError::Configuration),
        };
        let required = |name: &str| {
            std::env::var(name)
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or(StoreError::Configuration)
        };
        let seconds = |name: &str| {
            required(name)?
                .parse::<i64>()
                .ok()
                .filter(|value| *value > 0)
                .ok_or(StoreError::Configuration)
        };
        let client = redis::Client::open(url).map_err(|_| StoreError::Configuration)?;
        let namespace = required("FORETOKEN_RESPONSE_STORE_NAMESPACE")?;
        let connection = client.get_connection_info();
        // Completion cleanup must distinguish caches without retaining passwords or API credentials.
        let completion_prefix = format!(
            "api:{}:",
            json!([connection.addr.to_string(), connection.redis.db])
        );
        Ok(Some(Arc::new(Self {
            client,
            connection: OnceCell::new(),
            prefix: format!("foretoken:api:{namespace}:"),
            completion_prefix,
            ledger: ledger.map(foretoken_request_ledger::RequestLedger::storage_identity),
            pod: required("FORETOKEN_POD_UID")?,
            instance: Uuid::new_v4().to_string(),
            execution_scope: required("FORETOKEN_FRONTEND_UID")?,
            retention_seconds: seconds("FORETOKEN_RESPONSE_RETENTION_SECONDS")?,
            replay_seconds: seconds("FORETOKEN_RESPONSE_REPLAY_RETENTION_SECONDS")?,
        })))
    }

    /// Resolves a public item reference under the same caller scope as saved responses.
    pub(crate) async fn item(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        let body = self.transition("item", caller, json!({"id":id})).await?;
        let mut value = decode_json(body)?;
        value["id"] = json!(id);
        Ok(value)
    }

    /// Identifies this cache's completion consumers without exposing its connection credentials.
    pub(crate) fn completion_prefix(&self) -> &str {
        &self.completion_prefix
    }

    /// Locates a response in this service's cache partition for completion and recovery.
    pub(crate) fn response_ref(&self, id: &str) -> ResponseRef {
        ResponseRef {
            scope_prefix: self.prefix.clone(),
            id: id.to_owned(),
        }
    }

    /// Associates retained execution evidence with the cached response that consumes it.
    pub(crate) fn completion_consumer(&self, id: &str) -> String {
        format!("{}{}", self.completion_prefix, json!(self.response_ref(id)))
    }

    /// Checks physical record ownership before reclaiming a completed receipt after cache loss.
    pub(crate) async fn has_response(&self, response: &ResponseRef) -> Result<bool, StoreError> {
        self.transition("exists", &None, json!(response)).await
    }

    /// Runs a service-scoped atomic transition using the shared upstream Redis connection.
    async fn transition<T: redis::FromRedisValue>(
        &self,
        action: &str,
        caller: &Caller,
        input: Value,
    ) -> Result<T, StoreError> {
        let connection = self
            .connection
            .get_or_try_init(|| async {
                let mut connection = self
                    .client
                    .get_connection_manager_with_config(
                        ConnectionManagerConfig::new()
                            .set_connection_timeout(Duration::from_secs(5))
                            .set_max_delay(1_000),
                    )
                    .await?;
                self.invoke::<()>(&mut connection, "register", &None, json!({}))
                    .await?;
                Ok::<_, StoreError>(connection)
            })
            .await?;
        self.invoke(&mut connection.clone(), action, caller, input)
            .await
    }

    /// Keeps the script's domain errors separate from transport errors and malformed cache data.
    async fn invoke<T: redis::FromRedisValue>(
        &self,
        connection: &mut ConnectionManager,
        action: &str,
        caller: &Caller,
        input: Value,
    ) -> Result<T, StoreError> {
        let (status, value): (String, redis::Value) = TRANSITION
            .key(&self.prefix)
            .arg(action)
            .arg(serde_json::to_string(caller).expect("caller serialization"))
            .arg(input.to_string())
            .arg(&self.pod)
            .arg(&self.instance)
            .arg(self.retention_seconds.max(self.replay_seconds))
            .invoke_async(connection)
            .await?;
        match status.as_str() {
            "ok" => redis::from_redis_value(&value).map_err(|_| StoreError::Corrupt),
            "not_found" => Err(StoreError::NotFound),
            "invalid" => Err(StoreError::Invalid(
                redis::from_redis_value(&value).map_err(|_| StoreError::Corrupt)?,
            )),
            "conversation_busy" => Err(StoreError::ConversationBusy),
            "response_busy" => Err(StoreError::ResponseBusy),
            "changed" => Err(StoreError::Changed),
            "stream_expired" => Err(StoreError::StreamExpired),
            _ => Err(StoreError::Corrupt),
        }
    }
}

/// Encodes items for atomic reference resolution; a repeated reference receives its own cursor ID.
fn cache_items(items: &[Value]) -> Result<Vec<CachedItem>, StoreError> {
    items
        .iter()
        .map(|item| {
            let reference = item.get("type").and_then(Value::as_str) == Some("item_reference");
            let id = item.get("id").and_then(Value::as_str).ok_or_else(|| {
                if reference {
                    StoreError::Invalid("item_reference requires id".into())
                } else {
                    StoreError::Corrupt
                }
            })?;
            Ok(CachedItem {
                id: id.to_owned(),
                copy_id: format!("item_{}", Uuid::new_v4().simple()),
                body: (!reference).then(|| item.to_string()),
            })
        })
        .collect()
}

fn decode_json(body: String) -> Result<Value, StoreError> {
    serde_json::from_str(&body).map_err(|_| StoreError::Corrupt)
}

/// Restores occurrence IDs while preserving the original JSON content and numeric values.
fn decode_items(rows: Vec<(String, String)>) -> Result<Vec<Value>, StoreError> {
    rows.into_iter()
        .map(|(id, body)| {
            let mut item = decode_json(body)?;
            item.as_object_mut()
                .ok_or(StoreError::Corrupt)?
                .insert("id".into(), json!(id));
            Ok(item)
        })
        .collect()
}

/// Assigns public item IDs only to new inputs; inherited items retain their original identities.
pub(crate) fn input_items(input: Value) -> Result<Vec<Value>, StoreError> {
    let values = match input {
        Value::String(text) => vec![
            json!({"type":"message","role":"user","content":[{"type":"input_text","text":text}]}),
        ],
        Value::Array(items) => items,
        _ => {
            return Err(StoreError::Invalid(
                "input must be text or an item array".into(),
            ));
        }
    };
    values
        .into_iter()
        .map(|mut item| {
            let object = item
                .as_object_mut()
                .ok_or_else(|| StoreError::Invalid("input items must be objects".into()))?;
            if object.get("type").and_then(Value::as_str) == Some("item_reference") {
                return Ok(item);
            }
            if !object.contains_key("type") && object.contains_key("role") {
                object.insert("type".into(), json!("message"));
            }
            object.insert(
                "id".into(),
                json!(format!("item_{}", Uuid::new_v4().simple())),
            );
            Ok(item)
        })
        .collect()
}

impl PageQuery {
    /// Validates public cursor bounds before the owning cache query.
    pub(crate) fn bounds(&self, default_order: Order) -> Result<(usize, Order), StoreError> {
        let limit = self.limit.unwrap_or(20);
        if !(1..=100).contains(&limit) {
            return Err(StoreError::Invalid(
                "limit must be between 1 and 100".into(),
            ));
        }
        if self.after.is_some() && self.before.is_some() {
            return Err(StoreError::Invalid(
                "after and before cannot be combined".into(),
            ));
        }
        Ok((limit, self.order.unwrap_or(default_order)))
    }
}

/// Builds the common cursor query for response inputs, conversation items, and chat messages.
fn page_input(
    id: &str,
    query: &PageQuery,
    default_order: Order,
) -> Result<(usize, Value), StoreError> {
    let (limit, order) = query.bounds(default_order)?;
    Ok((
        limit,
        json!({"id":id,"limit":limit,"order":order,
        "cursor":query.after.as_deref().or(query.before.as_deref()),
        "after":query.after.is_some()}),
    ))
}

/// Encodes the common API list envelope without exposing cache cursor state.
pub(crate) fn list_page(mut data: Vec<Value>, limit: usize) -> Value {
    let has_more = data.len() > limit;
    data.truncate(limit);
    json!({"object":"list", "first_id":data.first().and_then(|item| item.get("id")),
        "last_id":data.last().and_then(|item| item.get("id")), "has_more":has_more, "data":data})
}
