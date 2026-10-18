-- SPDX-License-Identifier: Apache-2.0
-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

-- Immutable items are shared by response branches and conversation membership.
CREATE TABLE foretoken_api_items (
    namespace TEXT NOT NULL,
    id TEXT NOT NULL,
    caller TEXT,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (namespace, id)
);

CREATE TABLE foretoken_api_conversations (
    namespace TEXT NOT NULL,
    id TEXT NOT NULL,
    caller TEXT,
    metadata JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    active_response TEXT,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (namespace, id)
);

CREATE TABLE foretoken_api_records (
    namespace TEXT NOT NULL,
    id TEXT NOT NULL,
    caller TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('response', 'chat')),
    model TEXT NOT NULL,
    body JSONB NOT NULL,
    status TEXT NOT NULL,
    stored BOOLEAN NOT NULL,
    background BOOLEAN NOT NULL,
    streamed BOOLEAN NOT NULL,
    conversation_id TEXT,
    execution_pod TEXT NOT NULL,
    execution_instance TEXT NOT NULL,
    reservation JSONB,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ,
    replay_until TIMESTAMPTZ,
    PRIMARY KEY (namespace, id),
    FOREIGN KEY (namespace, conversation_id)
        REFERENCES foretoken_api_conversations (namespace, id)
);
CREATE INDEX foretoken_api_records_listing
    ON foretoken_api_records (namespace, caller, kind, created_at, id);
CREATE INDEX foretoken_api_records_expiry
    ON foretoken_api_records (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX foretoken_api_records_execution
    ON foretoken_api_records (namespace, execution_pod, execution_instance)
    WHERE status IN ('queued', 'in_progress');

-- Stage 0 is inherited history, 1 is this turn's input, and 2 is its output.
CREATE TABLE foretoken_api_record_items (
    namespace TEXT NOT NULL,
    record_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    stage SMALLINT NOT NULL CHECK (stage BETWEEN 0 AND 2),
    position BIGINT NOT NULL,
    PRIMARY KEY (namespace, record_id, stage, position),
    UNIQUE (namespace, record_id, item_id),
    FOREIGN KEY (namespace, record_id)
        REFERENCES foretoken_api_records (namespace, id) ON DELETE CASCADE,
    FOREIGN KEY (namespace, item_id)
        REFERENCES foretoken_api_items (namespace, id)
);
CREATE INDEX foretoken_api_record_item_references
    ON foretoken_api_record_items (namespace, item_id);

-- Deleting a conversation does not delete its items.
CREATE TABLE foretoken_api_conversation_items (
    namespace TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    position BIGINT NOT NULL,
    PRIMARY KEY (namespace, conversation_id, position),
    UNIQUE (namespace, conversation_id, item_id),
    FOREIGN KEY (namespace, conversation_id)
        REFERENCES foretoken_api_conversations (namespace, id),
    FOREIGN KEY (namespace, item_id)
        REFERENCES foretoken_api_items (namespace, id)
);
CREATE INDEX foretoken_api_conversation_item_references
    ON foretoken_api_conversation_items (namespace, item_id);

CREATE TABLE foretoken_api_events (
    namespace TEXT NOT NULL,
    response_id TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    body JSONB NOT NULL,
    PRIMARY KEY (namespace, response_id, sequence),
    FOREIGN KEY (namespace, response_id)
        REFERENCES foretoken_api_records (namespace, id) ON DELETE CASCADE
);

-- A process replaces only its own Pod's registration after acquiring its listener.
CREATE TABLE foretoken_api_frontends (
    namespace TEXT NOT NULL,
    pod TEXT NOT NULL,
    instance TEXT NOT NULL,
    PRIMARY KEY (namespace, pod)
);
