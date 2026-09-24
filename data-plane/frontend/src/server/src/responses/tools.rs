// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request-local tool names and custom-text argument mapping for Responses clients.

use std::collections::BTreeMap;

use foretoken_chat::{AssistantMessage, AssistantMessageExt as _, ChatTool};
use serde_json::{Value, json};

use super::error::ApiError;

/// External identity retained while the shared function parser uses a unique flat name.
#[derive(Clone)]
pub(super) struct ToolName {
    pub name: String,
    pub namespace: Option<String>,
    pub custom: bool,
}

/// Resolve tool declarations once and restore their wire identity in output/history.
#[derive(Default)]
pub(super) struct ToolNames(BTreeMap<String, ToolName>);

impl ToolNames {
    /// Lower function, namespace and unconstrained custom-text tools into shared chat tools.
    pub fn lower(tools: &[Value]) -> Result<(Vec<ChatTool>, Self), ApiError> {
        let mut names = Self::default();
        let mut lowered = Vec::new();
        for tool in tools {
            if tool.get("type").and_then(Value::as_str) == Some("namespace") {
                let namespace = required_name(tool, "name")?;
                let nested = tool.get("tools").and_then(Value::as_array).ok_or_else(|| {
                    ApiError::invalid_request("namespace requires tools", Some("tools"))
                })?;
                for tool in nested {
                    names.push(tool, Some(namespace), &mut lowered)?;
                }
            } else {
                names.push(tool, None, &mut lowered)?;
            }
        }
        Ok((lowered, names))
    }

    fn push(
        &mut self,
        tool: &Value,
        namespace: Option<&str>,
        lowered: &mut Vec<ChatTool>,
    ) -> Result<(), ApiError> {
        let custom = match tool.get("type").and_then(Value::as_str) {
            Some("function") => false,
            Some("custom") => true,
            _ => {
                return Err(ApiError::invalid_request(
                    "Only client-owned function, namespace and custom-text tools are supported",
                    Some("tools"),
                ));
            }
        };
        if custom
            && tool
                .get("format")
                .is_some_and(|format| format.get("type").and_then(Value::as_str) != Some("text"))
        {
            return Err(ApiError::invalid_request(
                "Custom tool grammar constraints are not supported",
                Some("tools"),
            ));
        }
        let name = required_name(tool, "name")?;
        let internal = qualified(namespace, name);
        if self.0.contains_key(&internal) {
            return Err(ApiError::invalid_request(
                "Tool names must be unambiguous after namespace resolution",
                Some("tools"),
            ));
        }
        let parsed = if custom {
            ChatTool {
                name: internal.clone(),
                description: tool
                    .get("description")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                parameters: json!({"type":"object","properties":{"input":{"type":"string"}},"required":["input"],"additionalProperties":false}),
                strict: None,
            }
        } else {
            let mut parsed: ChatTool = serde_json::from_value(tool.clone())
                .map_err(|e| ApiError::invalid_request(e.to_string(), Some("tools")))?;
            parsed.name = internal.clone();
            parsed
        };
        self.0.insert(
            internal,
            ToolName {
                name: name.into(),
                namespace: namespace.map(str::to_owned),
                custom,
            },
        );
        lowered.push(parsed);
        Ok(())
    }

    /// Defer possible truncation until Done; ordinary function validation remains client-owned.
    pub fn complete_arguments(&self, name: &str, arguments: &str) -> bool {
        if !self.get(name).is_some_and(|tool| tool.custom) {
            return !serde_json::from_str::<Value>(arguments).is_err_and(|error| error.is_eof());
        }
        matches!(Self::custom_arguments_complete(arguments), Ok(true))
    }

    /// Distinguish a truncated custom wrapper from malformed JSON or a non-text payload.
    fn custom_arguments_complete(arguments: &str) -> Result<bool, ApiError> {
        let parsed: Value = match serde_json::from_str(arguments) {
            Ok(parsed) => parsed,
            Err(error) if error.is_eof() => return Ok(false),
            Err(_) => return Err(ApiError::invalid_request("Invalid tool output", None)),
        };
        if !parsed.get("input").is_some_and(Value::is_string) {
            return Err(ApiError::invalid_request(
                "Custom tool output requires a text input",
                None,
            ));
        }
        Ok(true)
    }

    /// Preserve budget-truncated custom calls without weakening their existing wrapper validation.
    pub fn validate_finished_tools(
        &self,
        message: &AssistantMessage,
        truncated: bool,
    ) -> Result<(), ApiError> {
        for call in message.tool_calls() {
            if self.get(&call.name).is_some_and(|tool| tool.custom)
                && !Self::custom_arguments_complete(&call.arguments)?
                && !truncated
            {
                return Err(ApiError::invalid_request("Incomplete tool output", None));
            }
        }
        Ok(())
    }

    /// Restore output tool identity; custom-text calls expose their raw input instead of JSON arguments.
    pub fn restore_item(&self, item: &mut Value) -> Result<(), ApiError> {
        if item.get("type").and_then(Value::as_str) != Some("function_call") {
            return Ok(());
        }
        let Some(name) = item.get("name").and_then(Value::as_str) else {
            return Ok(());
        };
        let Some(tool) = self.0.get(name) else {
            return Err(ApiError::invalid_request(
                "Model returned an undeclared tool",
                Some("tools"),
            ));
        };
        let object = item.as_object_mut().expect("tool call is an object");
        object.insert("name".into(), json!(tool.name));
        if let Some(namespace) = &tool.namespace {
            object.insert("namespace".into(), json!(namespace));
        }
        if tool.custom {
            object.insert("type".into(), json!("custom_tool_call"));
            let in_progress =
                object.remove("status").as_ref().and_then(Value::as_str) == Some("in_progress");
            let arguments = object
                .remove("arguments")
                .and_then(|v| v.as_str().map(str::to_owned))
                .unwrap_or_default();
            let input = if arguments.is_empty() && in_progress {
                String::new()
            } else {
                let parsed: Value = serde_json::from_str(&arguments)
                    .map_err(|_| ApiError::invalid_request("Invalid custom tool output", None))?;
                parsed
                    .get("input")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        ApiError::invalid_request("Custom tool output requires a text input", None)
                    })?
                    .into()
            };
            object.insert("input".into(), json!(input));
        }
        Ok(())
    }

    /// Find one lowered tool by its internal name when adapting streamed arguments.
    pub fn get(&self, name: &str) -> Option<&ToolName> {
        self.0.get(name)
    }
}

/// Reconstruct the parser name from a replayed client-owned call or named choice.
pub(super) fn qualified(namespace: Option<&str>, name: &str) -> String {
    namespace.map_or_else(|| name.into(), |namespace| format!("{namespace}.{name}"))
}

fn required_name<'a>(value: &'a Value, key: &str) -> Result<&'a str, ApiError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| {
            ApiError::invalid_request(format!("Tool {key} must be nonempty"), Some("tools"))
        })
}
