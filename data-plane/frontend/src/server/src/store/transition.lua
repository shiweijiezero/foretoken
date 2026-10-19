-- SPDX-License-Identifier: Apache-2.0
-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

-- Serializes API state transitions within one shared service cache. Public JSON stays opaque:
-- decoding and re-encoding it with Lua cjson would change empty arrays and large integers.
local expiry_index = 'foretoken:api:expiry'
local replay_index = 'foretoken:api:replay_expiry'
local pending_index = 'foretoken:api:pending'
local acknowledgement_index = 'foretoken:api:acknowledgements'
local action = ARGV[1]
local caller = ARGV[2]
local input = cjson.decode(ARGV[3])
-- Recovery names the original partition; public operations always use the caller's configured service.
local prefix = input.scope_prefix or KEYS[1]
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local function key(kind, id) return prefix .. kind .. ':' .. id end
local function ok(value) return {'ok', value or ''} end
local function missing() return {'not_found', ''} end
local function invalid(message) return {'invalid', message} end
local function pending(status) return status == 'queued' or status == 'in_progress' end
local function expired(object, field)
    local value = redis.call('HGET', object, field)
    return value and value ~= '' and tonumber(value) <= now
end
local function accessible(object)
    return redis.call('HGET', object, 'caller') == caller
        and redis.call('HGET', object, 'deleted') ~= '1'
        and not expired(object, 'expires_at')
end
local function record(id, kind)
    local object = key('record', id)
    if not accessible(object) or redis.call('HGET', object, 'kind') ~= kind then return nil end
    if kind == 'response' and redis.call('HGET', object, 'stored') ~= '1'
        and redis.call('HGET', object, 'background') ~= '1' then return nil end
    return object
end
local function conversation(id)
    local object = key('conversation', id)
    if redis.call('HGET', object, 'caller') ~= caller or redis.call('HGET', object, 'deleted') == '1'
        or (redis.call('HGET', object, 'active_response') == '' and expired(object, 'expires_at')) then return nil end
    return object
end
-- Items survive while any retained view owns them, including a running conversation turn.
local function item_body(id, owner)
    local item = key('item', id)
    if redis.call('HGET', item, 'caller') ~= owner then return nil end
    for _, list in ipairs(redis.call('SMEMBERS', item .. ':owners')) do
        local object = list:match('^(.*):[^:]+$')
        local active = redis.call('HGET', object, 'active_response')
        if not expired(object, 'expires_at') or (active and active ~= '') then
            return redis.call('HGET', item, 'body')
        end
    end
    return nil
end
local function rows(ids)
    local result = {}
    for _, id in ipairs(ids) do
        local body = redis.call('HGET', key('item', id), 'body')
        if not body then return nil end
        result[#result + 1] = {id, body}
    end
    return result
end
local function conversation_body(object, id)
    return {id, redis.call('HGET', object, 'metadata'), redis.call('HGET', object, 'created_at')}
end
local function release_items(object, scope)
    local ids = redis.call('LRANGE', object, 0, -1)
    for _, id in ipairs(ids) do
        local item = scope .. 'item:' .. id
        redis.call('SREM', item .. ':owners', object)
        if redis.call('SCARD', item .. ':owners') == 0 then
            redis.call('DEL', item, item .. ':owners')
        end
    end
    redis.call('DEL', object)
end
local function append_items(object, owner, items)
    for _, item in ipairs(items) do
        local item_key = key('item', item.id)
        if item.body then redis.call('HSET', item_key, 'caller', owner, 'body', item.body) end
        redis.call('SADD', item_key .. ':owners', object)
        redis.call('RPUSH', object, item.id)
    end
end
-- Resolve every reference before acquiring a conversation or changing any membership.
local function prepare_items(items, seen, owner)
    local result = {}
    for _, item in ipairs(items) do
        local body = item.body
        if body == cjson.null then
            body = item_body(item.id, owner)
            if not body then return nil end
        end
        local id = item.id
        if seen[id] then id = item.copy_id end
        seen[id] = true
        result[#result + 1] = {id = id, body = body}
    end
    return result
end
local function seen_items(ids)
    local seen = {}
    for _, id in ipairs(ids) do seen[id] = true end
    return seen
end
local function enqueue_expiry(object)
    local expiry = redis.call('HGET', object, 'expires_at')
    if expiry and expiry ~= '' then redis.call('ZADD', expiry_index, expiry, object) end
end
local function drop_record(object, id, scope)
    if redis.call('HGET', object, 'kind') == 'chat' then
        local owner = redis.call('HGET', object, 'caller')
        redis.call('ZREM', scope .. 'chats:' .. owner, id)
    end
    release_items(object .. ':input', scope)
    release_items(object .. ':output', scope)
    redis.call('DEL', object, object .. ':events')
    redis.call('SREM', pending_index, object)
    redis.call('SREM', acknowledgement_index, object)
    redis.call('ZREM', expiry_index, object)
    redis.call('ZREM', replay_index, object)
end
local function item_page(list)
    local length = redis.call('LLEN', list)
    local lower, upper = 0, length - 1
    if input.cursor ~= cjson.null then
        local position = redis.call('LPOS', list, input.cursor)
        if not position then return nil end
        if (input.order == 'asc') == input.after then lower = position + 1
        else upper = position - 1 end
    end
    if lower > upper then return {} end
    local ids
    if input.order == 'asc' then
        ids = redis.call('LRANGE', list, lower, math.min(upper, lower + input.limit))
    else
        ids = redis.call('LRANGE', list, math.max(lower, upper - input.limit), upper)
        local reversed = {}
        for index = #ids, 1, -1 do reversed[#reversed + 1] = ids[index] end
        ids = reversed
    end
    return rows(ids)
end
local function append_events(object, events)
    for _, event in ipairs(events) do redis.call('ZADD', object .. ':events', event[1], event[2]) end
end

-- Registration expires after an idle process leaves; membership, not this TTL, decides execution death.
local registration = KEYS[1] .. 'frontend:' .. ARGV[4]
if action == 'register' then
    redis.call('SET', registration, ARGV[5], 'EX', ARGV[6])
    return ok()
end
redis.call('SET', registration, ARGV[5], 'NX', 'EX', ARGV[6])
redis.call('EXPIRE', registration, ARGV[6])
if action == 'item' then
    local body = item_body(input.id, caller)
    if not body then return missing() end
    return ok(body)
end
-- Each response owns its expanded history independently of its predecessors.
if action == 'history' or action == 'response_inputs' then
    local object = record(input.id, 'response')
    if not object then return missing() end
    if action == 'response_inputs' then
        local result = item_page(object .. ':input')
        if not result then return invalid('the pagination cursor was not found') end
        return ok(result)
    end
    if pending(redis.call('HGET', object, 'status')) then return {'response_busy', ''} end
    local ids = redis.call('LRANGE', object .. ':input', 0, -1)
    for _, id in ipairs(redis.call('LRANGE', object .. ':output', 0, -1)) do ids[#ids + 1] = id end
    local result = rows(ids)
    if not result then return missing() end
    return ok(result)
end
if action == 'begin' then
    local history, conv = {}, nil
    if input.previous ~= cjson.null then
        local previous = record(input.previous, 'response')
        if not previous then return missing() end
        if pending(redis.call('HGET', previous, 'status')) then return {'response_busy', ''} end
        history = redis.call('LRANGE', previous .. ':input', 0, -1)
        for _, id in ipairs(redis.call('LRANGE', previous .. ':output', 0, -1)) do history[#history + 1] = id end
    end
    if input.conversation ~= cjson.null then
        conv = conversation(input.conversation)
        if not conv then return missing() end
        if redis.call('HGET', conv, 'active_response') ~= '' then return {'conversation_busy', ''} end
        history = redis.call('LRANGE', conv .. ':items', 0, -1)
    end
    local inherited = rows(history)
    if not inherited then return missing() end
    local prepared = prepare_items(input.items, seen_items(history), caller)
    if not prepared then return missing() end
    local object = key('record', input.id)
    redis.call('HSET', object, 'scope_prefix', prefix, 'caller', caller, 'kind', 'response', 'model', input.model,
        'body', input.body, 'status', 'queued', 'stored', input.stored, 'background', input.background,
        'streamed', input.streamed, 'conversation', conv and input.conversation or '', 'execution_pod', input.pod,
        'execution_instance', input.instance, 'reservation', input.reservation, 'ledger', input.ledger,
        'retention', input.retention, 'replay', input.replay, 'deadline', now + input.remaining_millis,
        'cancel_requested', '0', 'deleted', '0', 'version', '1', 'expires_at', '', 'replay_until', '')
    for _, id in ipairs(history) do
        redis.call('RPUSH', object .. ':input', id)
        redis.call('SADD', key('item', id) .. ':owners', object .. ':input')
    end
    append_items(object .. ':input', caller, prepared)
    for _, item in ipairs(prepared) do inherited[#inherited + 1] = {item.id, item.body} end
    if conv then
        redis.call('HSET', object, 'new_input_start', #history)
        redis.call('HSET', conv, 'active_response', input.id)
        redis.call('ZREM', expiry_index, conv)
    end
    redis.call('SADD', pending_index, object)
    return ok(inherited)
end
if action == 'discard' then
    local object = key('record', input.id)
    if redis.call('HGET', object, 'execution_instance') == input.instance
        and redis.call('HGET', object, 'status') == 'queued' then
        local conv_id = redis.call('HGET', object, 'conversation')
        if conv_id ~= '' then
            local conv = key('conversation', conv_id)
            if redis.call('HGET', conv, 'active_response') == input.id then
                redis.call('HSET', conv, 'active_response', '')
                enqueue_expiry(conv)
            end
        end
        drop_record(object, input.id, prefix)
    end
    return ok()
end
if action == 'response' then
    local object = record(input.id, 'response')
    if not object then return missing() end
    return ok({redis.call('HGET', object, 'body'), redis.call('HGET', object, 'status'),
        redis.call('HGET', object, 'background'), redis.call('HGET', object, 'streamed'),
        expired(object, 'replay_until') and '1' or '0'})
end
if action == 'cancel' then
    local object = record(input.id, 'response')
    if not object then return missing() end
    if redis.call('HGET', object, 'background') ~= '1' then return invalid('only background responses can be cancelled') end
    if pending(redis.call('HGET', object, 'status')) then redis.call('HSET', object, 'cancel_requested', '1') end
    return ok(redis.call('HGET', object, 'body'))
end
if action == 'delete_response' then
    local object = record(input.id, 'response')
    if not object then return missing() end
    redis.call('HSET', object, 'deleted', '1')
    if not pending(redis.call('HGET', object, 'status')) then
        redis.call('HSET', object, 'expires_at', now)
        if redis.call('HGET', object, 'reservation') == '' then enqueue_expiry(object) end
    end
    return ok()
end
if action == 'cancellation_requested' then
    local object = key('record', input.id)
    if redis.call('HGET', object, 'execution_instance') ~= input.instance then return missing() end
    local cancelled = redis.call('HGET', object, 'cancel_requested') == '1'
        or not pending(redis.call('HGET', object, 'status'))
    return ok(cancelled and 1 or 0)
end
if action == 'publish' then
    local object = key('record', input.id)
    if redis.call('HGET', object, 'execution_instance') ~= input.instance then return missing() end
    if not pending(redis.call('HGET', object, 'status')) then return {'response_busy', ''} end
    if input.body ~= '' then redis.call('HSET', object, 'body', input.body, 'status', input.status) end
    append_events(object, input.events)
    redis.call('HINCRBY', object, 'version', 1)
    return ok()
end
if action == 'failure_snapshot' then
    local object = key('record', input.id)
    if redis.call('EXISTS', object) == 0 then return ok({}) end
    if not pending(redis.call('HGET', object, 'status')) then return ok({}) end
    local last = redis.call('ZREVRANGE', object .. ':events', 0, 0, 'WITHSCORES')
    local next_sequence = #last == 0 and 0 or tonumber(last[2]) + 1
    return ok({redis.call('HGET', object, 'body'), redis.call('HGET', object, 'version'), tostring(next_sequence)})
end
if action == 'finish' then
    local object = key('record', input.id)
    if redis.call('EXISTS', object) == 0 then return missing() end
    if input.instance ~= '' and redis.call('HGET', object, 'execution_instance') ~= input.instance then return missing() end
    if not pending(redis.call('HGET', object, 'status')) then return ok(redis.call('HGET', object, 'body')) end
    if input.version ~= '' and redis.call('HGET', object, 'version') ~= input.version then return {'changed', ''} end
    local owner = redis.call('HGET', object, 'caller')
    local retention = tonumber(redis.call('HGET', object, 'retention'))
    local replay = tonumber(redis.call('HGET', object, 'replay'))
    local conv_id = redis.call('HGET', object, 'conversation')
    local conv, append = nil, {}
    if conv_id ~= '' then
        conv = key('conversation', conv_id)
        if redis.call('EXISTS', conv) == 0 then return missing() end
        if redis.call('HGET', conv, 'active_response') == input.id and redis.call('HGET', conv, 'deleted') ~= '1'
            and (input.status == 'completed' or input.status == 'incomplete' or #input.items > 0) then
            local originals = rows(redis.call('LRANGE', object .. ':input', redis.call('HGET', object, 'new_input_start'), -1))
            if not originals then return missing() end
            for _, item in ipairs(originals) do append[#append + 1] = {id = item[1], body = item[2]} end
            for _, item in ipairs(input.items) do append[#append + 1] = item end
        end
    end
    append_items(object .. ':output', owner, input.items)
    if conv and redis.call('HGET', conv, 'active_response') == input.id then
        append_items(conv .. ':items', owner, append)
        redis.call('HSET', conv, 'active_response', '')
        if redis.call('HGET', conv, 'deleted') ~= '1' then
            redis.call('HSET', conv, 'expires_at', now + retention * 1000)
        end
        enqueue_expiry(conv)
    end
    if redis.call('HGET', object, 'stored') ~= '1' then retention = replay end
    if redis.call('HGET', object, 'deleted') == '1'
        or (redis.call('HGET', object, 'stored') ~= '1' and redis.call('HGET', object, 'background') ~= '1') then retention = 0 end
    redis.call('HSET', object, 'body', input.body, 'status', input.status,
        'expires_at', now + retention * 1000, 'replay_until', now + replay * 1000)
    redis.call('HINCRBY', object, 'version', 1)
    append_events(object, input.events)
    redis.call('SREM', pending_index, object)
    redis.call('ZADD', replay_index, now + replay * 1000, object)
    if redis.call('HGET', object, 'reservation') == '' then enqueue_expiry(object)
    else redis.call('SADD', acknowledgement_index, object) end
    return ok(input.body)
end
if action == 'events' or action == 'has_event' then
    local object = key('record', input.id)
    if redis.call('EXISTS', object) == 0 then return missing() end
    if expired(object, 'replay_until') then return {'stream_expired', ''} end
    if action == 'has_event' then return ok(redis.call('ZCOUNT', object .. ':events', input.sequence, input.sequence)) end
    return ok(redis.call('ZRANGEBYSCORE', object .. ':events', '(' .. input.after, '+inf', 'LIMIT', 0, 256))
end
if action == 'pending' then
    local result = {}
    for _, object in ipairs(redis.call('SMEMBERS', pending_index)) do
        if redis.call('HGET', object, 'ledger') == input.ledger then
            local scope = redis.call('HGET', object, 'scope_prefix')
            local id = object:sub(#scope + #'record:' + 1)
            local pod = redis.call('HGET', object, 'execution_pod')
            local instance = redis.call('HGET', object, 'execution_instance')
            local current = redis.call('GET', scope .. 'frontend:' .. pod)
            result[#result + 1] = {scope, id, redis.call('HGET', object, 'reservation'), pod, instance,
                instance == input.instance and '1' or '0', current and current ~= instance and '1' or '0',
                expired(object, 'deadline') and '1' or '0'}
        end
    end
    return ok(result)
end
if action == 'pending_started' then
    local object = key('record', input.id)
    if redis.call('HGET', object, 'execution_instance') ~= input.instance then return ok('') end
    local status = redis.call('HGET', object, 'status')
    if not pending(status) then return ok('') end
    return ok(status == 'in_progress' and '1' or '0')
end
if action == 'pending_acknowledgements' then
    local result = {}
    for _, object in ipairs(redis.call('SMEMBERS', acknowledgement_index)) do
        if redis.call('HGET', object, 'ledger') == input.ledger then
            local scope = redis.call('HGET', object, 'scope_prefix')
            local id = object:sub(#scope + #'record:' + 1)
            result[#result + 1] = {scope, id, redis.call('HGET', object, 'reservation')}
        end
    end
    return ok(result)
end
if action == 'acknowledged' then
    local object = key('record', input.id)
    local status = redis.call('HGET', object, 'status')
    if status and not pending(status) then
        redis.call('HSET', object, 'reservation', '')
        redis.call('SREM', acknowledgement_index, object)
        enqueue_expiry(object)
    end
    return ok()
end
if action == 'exists' then return ok(redis.call('EXISTS', key('record', input.id))) end

-- Membership changes serialize with generation; metadata edits preserve the active turn.
if action == 'conversation_create' then
    local prepared = prepare_items(input.items, {}, caller)
    if not prepared then return missing() end
    local object = key('conversation', input.id)
    redis.call('HSET', object, 'scope_prefix', prefix, 'caller', caller, 'metadata', input.metadata, 'created_at', math.floor(now / 1000),
        'active_response', '', 'deleted', '0', 'expires_at', now + input.retention * 1000)
    append_items(object .. ':items', caller, prepared)
    enqueue_expiry(object)
    return ok(conversation_body(object, input.id))
end
if action == 'conversation_get' or action == 'conversation_update' or action == 'conversation_delete'
    or action == 'conversation_items' or action == 'conversation_append' or action == 'conversation_get_item'
    or action == 'conversation_delete_item' then
    local object = conversation(input.id)
    if not object then return missing() end
    local active = redis.call('HGET', object, 'active_response')
    if action == 'conversation_get' then return ok(conversation_body(object, input.id)) end
    if action == 'conversation_update' then
        redis.call('HSET', object, 'metadata', input.metadata, 'expires_at', now + input.retention * 1000)
        if active == '' then enqueue_expiry(object) end
        return ok(conversation_body(object, input.id))
    end
    if action == 'conversation_delete' then
        redis.call('HSET', object, 'deleted', '1')
        return ok()
    end
    if action == 'conversation_items' then
        local result = item_page(object .. ':items')
        if not result then return invalid('the pagination cursor was not found') end
        return ok(result)
    end
    if action == 'conversation_get_item' then
        if not redis.call('LPOS', object .. ':items', input.item) then return missing() end
        return ok(redis.call('HGET', key('item', input.item), 'body'))
    end
    if active ~= '' then return {'conversation_busy', ''} end
    if action == 'conversation_append' then
        local ids = redis.call('LRANGE', object .. ':items', 0, -1)
        local prepared = prepare_items(input.items, seen_items(ids), caller)
        if not prepared then return missing() end
        append_items(object .. ':items', caller, prepared)
        local result = {}
        for _, item in ipairs(prepared) do result[#result + 1] = {item.id, item.body} end
        redis.call('HSET', object, 'expires_at', now + input.retention * 1000)
        enqueue_expiry(object)
        return ok(result)
    end
    if not redis.call('LPOS', object .. ':items', input.item) then return missing() end
    redis.call('LREM', object .. ':items', 0, input.item)
    local item = key('item', input.item)
    redis.call('SREM', item .. ':owners', object .. ':items')
    if redis.call('SCARD', item .. ':owners') == 0 then redis.call('DEL', item, item .. ':owners') end
    redis.call('HSET', object, 'expires_at', now + input.retention * 1000)
    enqueue_expiry(object)
    return ok(conversation_body(object, input.id))
end
-- Chat archives are terminal; metadata replacement compares the original opaque JSON before writing.
if action == 'chat_save' then
    local object = key('record', input.id)
    redis.call('HSET', object, 'scope_prefix', prefix, 'caller', caller, 'kind', 'chat', 'model', input.model, 'body', input.body,
        'status', 'completed', 'deleted', '0', 'reservation', '', 'expires_at', now + input.retention * 1000)
    append_items(object .. ':input', caller, input.items)
    redis.call('ZADD', prefix .. 'chats:' .. caller, input.created_at, input.id)
    enqueue_expiry(object)
    return ok()
end
if action == 'chat_get' or action == 'chat_messages' or action == 'chat_update' or action == 'chat_delete' then
    local object = record(input.id, 'chat')
    if not object then return missing() end
    if action == 'chat_get' then return ok(redis.call('HGET', object, 'body')) end
    if action == 'chat_messages' then
        local result = item_page(object .. ':input')
        if not result then return invalid('the pagination cursor was not found') end
        return ok(result)
    end
    if action == 'chat_update' then
        if redis.call('HGET', object, 'body') ~= input.previous_body then return {'changed', ''} end
        redis.call('HSET', object, 'body', input.body)
        return ok(input.body)
    end
    drop_record(object, input.id, prefix)
    return ok()
end
if action == 'chat_list' then
    local index_key = prefix .. 'chats:' .. caller
    local direction = input.order == 'asc' and 'ZRANGE' or 'ZREVRANGE'
    local function matches(id)
        local object = record(id, 'chat')
        if not object then return nil end
        if input.model ~= cjson.null and redis.call('HGET', object, 'model') ~= input.model then return nil end
        local body = redis.call('HGET', object, 'body')
        if input.metadata ~= cjson.null then
            local metadata = cjson.decode(body).metadata
            if type(metadata) ~= 'table' then return nil end
            for name, value in pairs(input.metadata) do if metadata[name] ~= value then return nil end end
        end
        return body
    end
    local offset = 0
    if input.cursor ~= cjson.null then
        if not matches(input.cursor) then return invalid('the pagination cursor was not found') end
        offset = redis.call(input.order == 'asc' and 'ZRANK' or 'ZREVRANK', index_key, input.cursor)
        if not offset then return invalid('the pagination cursor was not found') end
        offset = offset + 1
    end
    local result = {}
    while #result <= input.limit do
        local ids = redis.call(direction, index_key, offset, offset + input.limit)
        if #ids == 0 then break end
        for _, id in ipairs(ids) do
            local body = matches(id)
            if body then result[#result + 1] = body end
            if #result > input.limit then break end
        end
        offset = offset + #ids
    end
    return ok(result)
end
if action == 'collect' then
    -- Any frontend can reclaim expired cache objects, including those from a stopped service.
    -- Public reads enforce expiry independently of these bounded collection batches.
    for _, object in ipairs(redis.call('ZRANGEBYSCORE', replay_index, '-inf', now, 'LIMIT', 0, 256)) do
        redis.call('DEL', object .. ':events')
        redis.call('ZREM', replay_index, object)
    end
    for _, object in ipairs(redis.call('ZRANGEBYSCORE', expiry_index, '-inf', now, 'LIMIT', 0, 256)) do
        local scope = redis.call('HGET', object, 'scope_prefix')
        if redis.call('HEXISTS', object, 'kind') == 1 then
            drop_record(object, object:sub(#scope + #'record:' + 1), scope)
        else
            release_items(object .. ':items', scope)
            redis.call('DEL', object)
            redis.call('ZREM', expiry_index, object)
        end
    end
    return ok()
end
return redis.error_reply('unknown API state transition')
