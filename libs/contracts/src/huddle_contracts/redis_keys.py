"""Redis key names (Deep Dive §4)."""

AGENT_TRIGGERS_STREAM = "agent:triggers"
AGENT_TRIGGERS_GROUP = "agent-workers"
AGENT_TIMERS_ZSET = "agent:timers"  # member: session_id, score: due epoch seconds

CONN_TTL_SECONDS = 60
CONN_REFRESH_SECONDS = 20
GW_ALIVE_TTL_SECONDS = 30


def conn(connection_id: str) -> str:
    """Hash: user_id, device_id, gateway_id, connected_at, last_heartbeat."""
    return f"conn:{connection_id}"


def user_conns(user_id: str) -> str:
    """Sorted set of connection_ids scored by expiry time."""
    return f"user_conns:{user_id}"


def gw_alive(gateway_id: str) -> str:
    return f"gw_alive:{gateway_id}"


def gw_channel(gateway_id: str) -> str:
    """Pub/sub channel carrying FanoutEnvelope JSON."""
    return f"gw:{gateway_id}"


def rate_limit(kind: str, subject: str) -> str:
    return f"rl:{kind}:{subject}"
