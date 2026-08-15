-- Turn a syslog line into the shape the ingestion field map reads.
--
-- Syslog carries no event type, so it has to be derived from the message text. This is the one
-- piece of guesswork in the bridge, and the first place to look when a ticket arrives as
-- Unclassified: add a pattern here rather than widening the app's configuration.
--
-- The names on the right are the seeded event types from migration 0002. They have to match
-- exactly, or the pre-filter falls back to `defaults.event_type`.

local PATTERNS = {
    {"is down",                     "Interface Down"},
    {"oper%-state.*down",           "Interface Down"},
    {"bgp.*[Nn]eighbor.*[Dd]own",   "BGP Session Down"},
    {"bgp.*session.*clear",         "BGP Session Down"},
    {"[Uu]nreachable",              "Device Unreachable"},
    {"[Kk]eepalive.*expired",       "Device Unreachable"},
    {"[Oo]ptical.*threshold",       "Optical Degradation"},
    {"[Cc]pu.*threshold",           "High CPU Utilization"},
    {"[Mm]emory.*threshold",        "High Memory Utilization"},
    {"[Cc]ommit.*accepted",         "Configuration Drift"},
    {"[Ff]an.*fail",                "Hardware Alarm"},
    {"[Pp]ower.*fail",              "Hardware Alarm"},
    {"[Tt]emperature.*exceed",      "Hardware Alarm"},
}

-- The interface name, where the message names one. The dedup key uses it, so that two different
-- interfaces flapping on one device are two tickets rather than one.
local function interface_of(message)
    return string.match(message, "(ethernet%-%d+/%d+)")
        or string.match(message, "(%a+%d+/%d+/%d+)")
        or ""
end

local function type_of(message)
    for _, pair in ipairs(PATTERNS) do
        if string.find(message, pair[1]) then
            return pair[2]
        end
    end
    return "Unclassified"
end

function classify(tag, timestamp, record)
    local message = record["message"] or ""

    -- Syslog priority is facility * 8 + severity; the app's severity_map is keyed on the severity.
    local severity = "6"
    if record["pri"] ~= nil then
        severity = tostring(tonumber(record["pri"]) % 8)
    end

    record["event"] = {
        type = type_of(message),
        severity = severity,
    }
    record["interface"] = interface_of(message)

    -- 2 means "the record was modified"; the timestamp is left as the input parsed it.
    return 2, timestamp, record
end
