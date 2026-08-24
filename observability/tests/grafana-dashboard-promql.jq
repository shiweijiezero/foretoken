# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

def substitute_variables:
  gsub("\\$\\{namespace:regex\\}"; ".*")
  | gsub("\\$\\{frontend_service:regex\\}"; ".+")
  | gsub("\\$\\{model_role:regex\\}"; ".+")
  | gsub("\\$\\{model_group:regex\\}"; ".+");

def variable_expression:
  if startswith("query_result(") then
    sub("^query_result\\("; "") | sub("\\)$"; "")
  elif startswith("label_values(") then
    capture(
      "^label_values\\((?<expression>.*),[[:space:]]*[a-zA-Z_:][a-zA-Z0-9_:]*\\)$"
    ).expression
  else
    empty
  end;

(
  [.panels[].targets[].expr]
  + [
      .templating.list[]
      | select(.type == "query")
      | (.query.query // .query)
      | variable_expression
    ]
)
| map(substitute_variables)
| to_entries
| {
    groups: [
      {
        name: "foretoken.dashboard.validation",
        rules: map({
          record: "foretoken_dashboard_validation_\(.key)",
          expr: .value
        })
      }
    ]
  }
