// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! PostgreSQL ownership for saved API objects, immutable history, and response events.

mod completions;
mod conversations;
mod records;

use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use sqlx::{PgPool, Postgres, Row, Transaction};
use thiserror::Error;
use tokio::sync::OnceCell;
use uuid::Uuid;

pub(crate) use records::NewResponse;

/// Storage failures stay separate from model execution and never expose SQL or credentials.
#[derive(Debug, Error)]
pub(crate) enum StoreError {
    #[error("API state storage is not configured")]
    Disabled,
    #[error("invalid API state storage configuration")]
    Configuration,
    #[error("API state storage is unavailable")]
    Database(#[from] sqlx::Error),
    #[error("API state schema could not be initialized")]
    Migration(#[from] sqlx::migrate::MigrateError),
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
    #[error("stored API state is invalid")]
    Corrupt,
}

/// One service's database connection and process registration, independent of model snapshots.
pub(crate) struct ApiStore {
    pool: PgPool,
    namespace: String,
    pod: String,
    instance: String,
    execution_scope: String,
    retention_seconds: i64,
    replay_seconds: i64,
    initialized: OnceCell<()>,
}

/// The ingress-resolved caller associated with a stored API object.
pub(crate) type Caller = Option<String>;

impl ApiStore {
    /// Opens the controller-projected connection lazily; stateless requests need no database.
    pub(crate) fn from_env() -> Result<Option<Arc<Self>>, StoreError> {
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
        let pool = PgPoolOptions::new()
            .connect_lazy(&url)
            .map_err(|_| StoreError::Configuration)?;
        Ok(Some(Arc::new(Self {
            pool,
            namespace: required("FORETOKEN_RESPONSE_STORE_NAMESPACE")?,
            pod: required("FORETOKEN_POD_UID")?,
            instance: Uuid::new_v4().to_string(),
            execution_scope: required("FORETOKEN_FRONTEND_UID")?,
            retention_seconds: seconds("FORETOKEN_RESPONSE_RETENTION_SECONDS")?,
            replay_seconds: seconds("FORETOKEN_RESPONSE_REPLAY_RETENTION_SECONDS")?,
            initialized: OnceCell::new(),
        })))
    }

    /// Applies embedded migrations once, with SQLx's cross-process migration lock.
    async fn pool(&self) -> Result<&PgPool, StoreError> {
        self.initialized
            .get_or_try_init(|| async {
                sqlx::migrate!("./migrations").run(&self.pool).await?;
                sqlx::query(
                    "INSERT INTO foretoken_api_frontends (namespace, pod, instance) VALUES ($1,$2,$3) \
                     ON CONFLICT (namespace,pod) DO UPDATE SET instance=EXCLUDED.instance",
                )
                .bind(&self.namespace)
                .bind(&self.pod)
                .bind(&self.instance)
                .execute(&self.pool)
                .await?;
                Ok::<(), StoreError>(())
            })
            .await?;
        Ok(&self.pool)
    }

    /// Reads immutable items after the caller's response or conversation access was checked.
    async fn record_items(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        record: &str,
    ) -> Result<Vec<Value>, StoreError> {
        let rows = sqlx::query(
            "SELECT i.body FROM foretoken_api_record_items r \
             JOIN foretoken_api_items i ON i.namespace=r.namespace AND i.id=r.item_id \
             WHERE r.namespace=$1 AND r.record_id=$2 \
             ORDER BY r.stage,r.position",
        )
        .bind(&self.namespace)
        .bind(record)
        .fetch_all(&mut **transaction)
        .await?;
        rows.into_iter()
            .map(|row| Ok(row.try_get("body")?))
            .collect()
    }

    /// Reads one input page after the caller has locked and authorized its owning record.
    async fn record_input_page(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        record: &str,
        query: &PageQuery,
        default_order: Order,
    ) -> Result<Value, StoreError> {
        let (limit, order) = query.bounds(default_order)?;
        let direction = match order {
            Order::Asc => "ASC",
            Order::Desc => "DESC",
        };
        let comparison = if matches!(order, Order::Asc) == query.after.is_some() {
            ">"
        } else {
            "<"
        };
        let cursor: Option<(i16, i64)> =
            if let Some(item) = query.after.as_deref().or(query.before.as_deref()) {
                let row = sqlx::query(
                    "SELECT stage,position FROM foretoken_api_record_items \
                 WHERE namespace=$1 AND record_id=$2 AND item_id=$3 AND stage<2",
                )
                .bind(&self.namespace)
                .bind(record)
                .bind(item)
                .fetch_optional(&mut **transaction)
                .await?
                .ok_or_else(|| StoreError::Invalid("the pagination cursor was not found".into()))?;
                Some((row.try_get("stage")?, row.try_get("position")?))
            } else {
                None
            };
        let sql = format!(
            "SELECT i.body FROM foretoken_api_record_items r \
             JOIN foretoken_api_items i ON i.namespace=r.namespace AND i.id=r.item_id \
             WHERE r.namespace=$1 AND r.record_id=$2 AND r.stage<2 \
             AND ($3::SMALLINT IS NULL OR (r.stage,r.position) {comparison} ($3,$4)) \
             ORDER BY r.stage {direction},r.position {direction} LIMIT $5"
        );
        let items = sqlx::query_scalar(&sql)
            .bind(&self.namespace)
            .bind(record)
            .bind(cursor.map(|(stage, _)| stage))
            .bind(cursor.map(|(_, position)| position))
            .bind((limit + 1) as i64)
            .fetch_all(&mut **transaction)
            .await?;
        Ok(list_page(items, limit))
    }

    /// Creates immutable items and references in the same transaction as their owning object.
    async fn insert_record_items(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        record: &str,
        caller: &Caller,
        stage: i16,
        items: &[Value],
    ) -> Result<(), StoreError> {
        for (position, item) in items.iter().enumerate() {
            let id = item
                .get("id")
                .and_then(Value::as_str)
                .ok_or(StoreError::Corrupt)?;
            sqlx::query(
                "INSERT INTO foretoken_api_items (namespace,id,caller,body) VALUES ($1,$2,$3,$4) \
                 ON CONFLICT (namespace,id) DO NOTHING",
            )
            .bind(&self.namespace)
            .bind(id)
            .bind(caller)
            .bind(item)
            .execute(&mut **transaction)
            .await?;
            sqlx::query(
                "INSERT INTO foretoken_api_record_items \
                 (namespace,record_id,item_id,stage,position) VALUES ($1,$2,$3,$4,$5)",
            )
            .bind(&self.namespace)
            .bind(record)
            .bind(id)
            .bind(stage)
            .bind(position as i64)
            .execute(&mut **transaction)
            .await?;
        }
        Ok(())
    }

    /// Resolves public item references under the same access rules as saved responses.
    pub(crate) async fn item(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        sqlx::query_scalar(
            "SELECT body FROM foretoken_api_items \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)
    }
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

/// Cursor parameters shared by stored-object and item listings.
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PageQuery {
    pub after: Option<String>,
    pub before: Option<String>,
    pub limit: Option<usize>,
    pub order: Option<Order>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Order {
    Asc,
    Desc,
}

impl PageQuery {
    /// Validates OpenAI cursor bounds once before the owning SQL query.
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

/// Encodes the common API list envelope without exposing database cursor state.
pub(crate) fn list_page(mut data: Vec<Value>, limit: usize) -> Value {
    let has_more = data.len() > limit;
    data.truncate(limit);
    json!({"object":"list", "first_id":data.first().and_then(|item| item.get("id")),
        "last_id":data.last().and_then(|item| item.get("id")), "has_more":has_more, "data":data})
}
