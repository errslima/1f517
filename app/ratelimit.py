"""In-memory sliding-window rate limits. Single-process by design (see
HANDOFF stack notes); restart resets counters, which errs permissive."""
import time
from collections import deque, defaultdict
from . import config

_hits = defaultdict(deque)

def check(key: str, limit: int, window_secs: int):
    """Returns retry_after seconds if limited, else None (and records hit)."""
    now = time.time()
    q = _hits[key]
    while q and q[0] <= now - window_secs:
        q.popleft()
    if len(q) >= limit:
        return max(1, int(q[0] + window_secs - now))
    q.append(now)
    return None

def check_write(token_key: str):
    return check(f"w:{token_key}", config.WRITES_PER_HOUR, 3600)

def check_read(token_key: str | None, ip: str):
    if token_key:
        return check(f"r:{token_key}", config.READS_PER_HOUR_TOKEN, 3600)
    return check(f"ri:{ip}", config.READS_PER_HOUR_IP, 3600)

def check_register(ip: str):
    return check(f"reg:{ip}", config.REGISTRATIONS_PER_DAY_IP, 86400)
