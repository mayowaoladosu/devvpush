"""Atomic Redis-backed request limits for sensitive LayerRail interfaces."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from redis.asyncio import Redis

_SAFE_PART = re.compile(r"[^A-Za-z0-9_.:-]+")
_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimiter:
    @classmethod
    async def check(
        cls,
        redis: Redis,
        *,
        bucket: str,
        identity: object,
        limit: int,
        window_seconds: int,
        enabled: bool = True,
    ) -> RateLimitResult:
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        if not enabled:
            return RateLimitResult(True, limit, limit, 0)
        safe_bucket = _SAFE_PART.sub("-", str(bucket or "request"))[:80]
        identity_hash = hashlib.sha256(
            str(identity or "anonymous").encode("utf-8")
        ).hexdigest()[:32]
        key = f"layerrail:ratelimit:{safe_bucket}:{identity_hash}"
        count, ttl = await redis.eval(_SCRIPT, 1, key, window_seconds)
        count = int(count)
        ttl = max(1, int(ttl if int(ttl) > 0 else window_seconds))
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=0 if count <= limit else ttl,
        )
