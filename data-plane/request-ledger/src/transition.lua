-- SPDX-License-Identifier: Apache-2.0
-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

-- Each transition owns the reservation and all associated counters in one atomic operation.
local key = KEYS[1]
local action = ARGV[1]
local id = ARGV[2]
local input = cjson.decode(ARGV[3])

local function count(name)
    return tonumber(redis.call('HGET', key, name) or '0')
end

local function adjust(name, amount)
    if amount == 0 then return end
    local value = redis.call('HINCRBY', key, name, amount)
    if value == 0 then redis.call('HDEL', key, name) end
    if name:sub(1, 2) == 'a:' then adjust('active', amount) end
end

local function save(request_id, request)
    local field = 'r:' .. request_id
    if request.remaining == 0 and (not request.cancelled or request.retired) then
        redis.call('HDEL', key, field)
    else
        redis.call('HSET', key, field, cjson.encode(request))
    end
end

local function finish_slot(request, slot)
    if slot.state == 'completed' then return end
    if slot.state == 'waiting' or slot.state == 'reserved' then
        adjust('waiting', -1)
        adjust('w:' .. request.caller, -1)
    end
    if slot.state ~= 'waiting' then adjust('a:' .. request.caller, -1) end
    slot.state = 'completed'
    request.remaining = request.remaining - 1
end

-- Revocation fences unsubmitted work. Engine-owned work remains charged until its owner
-- confirms completion, even when its frontend process or Pod has disappeared.
local function cancel_slot(request, slot)
    if slot.state == 'accepted' then
        slot.state = 'cancelling'
    elseif slot.state == 'waiting' or slot.state == 'reserved' or slot.state == 'handoff' then
        finish_slot(request, slot)
    end
end

local function retire_frontends(keep)
    for _, field in ipairs(redis.call('HKEYS', key)) do
        if field:sub(1, 2) == 'r:' then
            local request = cjson.decode(redis.call('HGET', key, field))
            if not keep(request) then
                request.retired = true
                for _, slot in ipairs(request.slots) do cancel_slot(request, slot) end
                save(field:sub(3), request)
            end
        end
    end
end

local function finish_backend_slots(terminated)
    for _, field in ipairs(redis.call('HKEYS', key)) do
        if field:sub(1, 2) == 'r:' then
            local request = cjson.decode(redis.call('HGET', key, field))
            local changed = false
            for _, slot in ipairs(request.slots) do
                if (slot.state == 'accepted' or slot.state == 'cancelling')
                    and slot.backend_pod and terminated(slot) then
                    finish_slot(request, slot)
                    changed = true
                end
            end
            if changed then save(field:sub(3), request) end
        end
    end
end

if action == 'recover_backend' then
    local encoded_backends = redis.call('HGET', key, 'backends')
    if not encoded_backends or not cjson.decode(encoded_backends)[input.pod] then return 'ok' end
    local previous = count('b:' .. input.pod)
    if input.epoch < previous then return 'ok' end
    if input.epoch > previous then redis.call('HSET', key, 'b:' .. input.pod, input.epoch) end
    finish_backend_slots(function(slot)
        return slot.backend_pod == input.pod and slot.backend_epoch and slot.backend_epoch < input.epoch
    end)
    return 'ok'
end

-- Membership is controller-observed ownership, not an activation of candidate settings.
-- Separate revisions let recovery progress while a frontend rejects a routing configuration.
if action == 'publish' or action == 'publish_membership' then
    local version = tonumber(redis.call('HGET', key, 'version') or '-1')
    local membership_version = tonumber(redis.call('HGET', key, 'membership_version') or version)
    if action == 'publish' and input.version > version then
        redis.call('HSET', key, 'version', input.version, 'config', cjson.encode(input.config))
    end
    if input.version > membership_version then
        local frontends, backends = {}, {}
        for _, frontend in ipairs(input.frontends) do frontends[frontend] = true end
        for _, backend in ipairs(input.backends) do backends[backend] = true end
        redis.call('HSET', key, 'membership_version', input.version,
            'frontends', cjson.encode(frontends), 'backends', cjson.encode(backends))
        retire_frontends(function(request) return frontends[request.frontend] == true end)
        finish_backend_slots(function(slot) return not backends[slot.backend_pod] end)
        for _, field in ipairs(redis.call('HKEYS', key)) do
            if (field:sub(1, 2) == 'o:' and not frontends[field:sub(3)])
                or (field:sub(1, 2) == 'b:' and not backends[field:sub(3)]) then
                redis.call('HDEL', key, field)
            end
        end
    end
    return 'ok'
end

local encoded = redis.call('HGET', key, 'config')
if not encoded then return 'unavailable' end
local config = cjson.decode(encoded)
local frontends = cjson.decode(redis.call('HGET', key, 'frontends'))

-- Allocation and activation are separate. A delayed allocation from a dead process cannot
-- replace a newer active owner; an old activation is fenced by its smaller store-issued epoch.
if action == 'allocate_owner' then
    if not frontends[input.frontend] then return 'closed' end
    return tostring(redis.call('HINCRBY', key, 'owner_epoch', 1))
end
if action == 'register_owner' then
    if not frontends[input.frontend] then return 'closed' end
    local current = count('o:' .. input.frontend)
    if input.epoch < current then return 'closed' end
    if input.epoch > current then
        redis.call('HSET', key, 'o:' .. input.frontend, input.epoch)
        retire_frontends(function(request)
            return request.frontend ~= input.frontend or request.epoch == input.epoch
        end)
    end
    return 'ok'
end

if action == 'stats' then
    return cjson.encode({count('waiting'), count('active')})
end

local function rule_for(role, caller)
    if not config.roleRules or #config.roleRules == 0 then return {} end
    if caller == '' then return nil end
    for _, rule in ipairs(config.roleRules) do
        if rule.role == role then return rule end
    end
    return nil
end

local record = redis.call('HGET', key, 'r:' .. id)
local request = record and cjson.decode(record) or nil
if config.closed and (action == 'enqueue' or action == 'dispatch') then return 'closed' end

if action == 'enqueue' then
    if not frontends[input.frontend] or count('o:' .. input.frontend) ~= input.epoch then return 'closed' end
    if request then
        if request.cancelled or request.retired then return 'closed' end
        return 'ok'
    end
    local rule = rule_for(input.role, input.caller)
    if not rule then return 'role' end
    local units = input.units
    if units < 1 then return 'batch' end
    local limits = rule.perCaller
    if (config.maxWaitingRequests and units > config.maxWaitingRequests)
        or (limits and (units > limits.maxWaitingRequests or units > limits.maxConcurrentRequests)) then
        return 'batch'
    end
    if config.maxWaitingRequests and count('waiting') + units > config.maxWaitingRequests then return 'full' end
    if limits and count('w:' .. input.caller) + units > limits.maxWaitingRequests then return 'full' end
    request = {caller = input.caller, role = input.role, frontend = input.frontend, epoch = input.epoch,
        remaining = units, accepted = false, slots = {}}
    for slot = 1, units do request.slots[slot] = {state = 'waiting'} end
    adjust('waiting', units)
    adjust('w:' .. request.caller, units)
    save(id, request)
    return 'ok'
end

-- Only an uncertain enqueue needs a negative record. Normal completion has no replayable
-- enqueue left; these fences are retired with their frontend process ownership epoch.
if action == 'cancel_entry' then
    if not request then
        if not frontends[input.frontend] or count('o:' .. input.frontend) ~= input.epoch then return 'ok' end
        request = {caller = input.caller, frontend = input.frontend, epoch = input.epoch,
            remaining = 0, slots = {}}
    end
    request.cancelled = true
    for _, slot in ipairs(request.slots) do cancel_slot(request, slot) end
    save(id, request)
    return 'ok'
end

if not request then
    if action == 'drop' or action == 'complete' then return 'ok' end
    return 'closed'
end

if action == 'dispatch' then
    if request.cancelled or request.retired then return 'closed' end
    local rule = rule_for(request.role, request.caller)
    if not rule then return 'role' end
    local needed = 0
    for _, slot in ipairs(request.slots) do
        if slot.state == 'waiting' then needed = needed + 1 end
    end
    if rule.perCaller and needed > rule.perCaller.maxConcurrentRequests then return 'batch' end
    if rule.perCaller and count('a:' .. request.caller) + needed > rule.perCaller.maxConcurrentRequests then return 'busy' end
    for _, slot in ipairs(request.slots) do
        if slot.state == 'waiting' then slot.state = 'reserved' end
    end
    adjust('a:' .. request.caller, needed)
    save(id, request)
    return 'ok'
end

if action == 'retry' then
    if request.accepted then return 'accepted' end
    local returned = 0
    for _, slot in ipairs(request.slots) do
        if slot.state == 'reserved' then
            slot.state = 'waiting'
            returned = returned + 1
        end
    end
    adjust('a:' .. request.caller, -returned)
    save(id, request)
    return 'ok'
end

local slot = request.slots[input.slot + 1]
if not slot then return 'closed' end

if action == 'accept' then
    local backends = cjson.decode(redis.call('HGET', key, 'backends'))
    if not backends[input.backend_pod] then return 'backend_unavailable' end
    local epoch = count('b:' .. input.backend_pod)
    if input.backend_epoch < epoch then return 'closed' end
    if slot.finished_owners and slot.finished_owners[input.owner] then return 'closed' end
    if slot.state == 'accepted' and slot.owner == input.owner then return 'ok' end
    if slot.state ~= 'reserved' and slot.state ~= 'handoff' then return 'closed' end
    if slot.state == 'reserved' then
        adjust('waiting', -1)
        adjust('w:' .. request.caller, -1)
    end
    if input.backend_epoch > epoch then redis.call('HSET', key, 'b:' .. input.backend_pod, input.backend_epoch) end
    slot.state = 'accepted'
    slot.owner = input.owner
    slot.backend_pod = input.backend_pod
    slot.backend_epoch = input.backend_epoch
    request.accepted = true
elseif action == 'drop' then
    cancel_slot(request, slot)
elseif action == 'reject_before_submission' then
    if not (slot.finished_owners and slot.finished_owners[input.owner]) then
        if slot.state == 'waiting' or slot.state == 'reserved' or slot.state == 'handoff'
            or ((slot.state == 'accepted' or slot.state == 'cancelling') and slot.owner == input.owner) then
            finish_slot(request, slot)
        end
    end
elseif action == 'complete' then
    if (slot.state == 'accepted' or slot.state == 'cancelling') and slot.owner == input.owner then
        slot.finished_owners = slot.finished_owners or {}
        slot.finished_owners[input.owner] = true
        if input.final_stage or slot.state == 'cancelling' then
            finish_slot(request, slot)
        else
            slot.state = 'handoff'
        end
    end
elseif action == 'status' then
    return slot.state
else
    return redis.error_reply('unknown reservation transition')
end
save(id, request)
return 'ok'
