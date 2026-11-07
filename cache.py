"""Redis caching layer with in-memory fallback."""
import json
import time
from typing import Any, Optional
from functools import wraps
import os

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# In-memory fallback cache
_memory_cache: dict[str, tuple[Any, float]] = {}
_memory_max_size = 1000

_redis_client: Optional["redis.Redis"] = None


class AttrDict(dict):
    """
    Dictionary that also supports attribute access.

    Cached values are stored as plain JSON, so nested records come back as
    dicts.  Most of the application, however, reads them with attributes
    (``room.participants``, ``message.sender_id`` ...).  AttrDict bridges the
    gap so an object fetched from the database (ORM instance) and the same
    object fetched from the cache behave the same way.

    It is a real ``dict`` subclass, so ``isinstance(x, dict)`` stays True and
    item access (``x["id"]``), ``.get()`` and friends keep working.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc


def wrap(value: Any) -> Any:
    """Recursively convert dicts/lists into AttrDict-compatible structures."""
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict({k: wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap(v) for v in value]
    if isinstance(value, tuple):
        return tuple(wrap(v) for v in value)
    return value


def get_redis_url() -> str:
    """Get Redis URL from environment or use default."""
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


async def init_redis() -> bool:
    """Initialize Redis connection. Returns True if successful."""
    global _redis_client
    if not REDIS_AVAILABLE:
        return False
    try:
        _redis_client = redis.from_url(
            get_redis_url(),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await _redis_client.ping()
        return True
    except Exception:
        _redis_client = None
        return False


async def close_redis():
    """Close Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


async def cache_get(key: str) -> Optional[Any]:
    """Get value from cache."""
    # Try Redis first
    if _redis_client:
        try:
            val = await _redis_client.get(key)
            if val is not None:
                return wrap(json.loads(val))
        except Exception:
            pass

    # Fallback to in-memory
    entry = _memory_cache.get(key)
    if entry:
        value, expiry = entry
        if expiry > time.time():
            return wrap(value)
        else:
            del _memory_cache[key]
    return None


async def cache_set(key: str, value: Any, ttl: int = 300) -> bool:
    """Set value in cache with TTL in seconds."""
    # Try Redis first
    if _redis_client:
        try:
            await _redis_client.setex(key, ttl, json.dumps(value, default=str))
            return True
        except Exception:
            pass

    # Fallback to in-memory
    _enforce_memory_limit()
    _memory_cache[key] = (value, time.time() + ttl)
    return True


async def cache_delete(key: str) -> bool:
    """Delete key from cache."""
    success = False
    if _redis_client:
        try:
            await _redis_client.delete(key)
            success = True
        except Exception:
            pass

    if key in _memory_cache:
        del _memory_cache[key]
        success = True
    return success


async def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching pattern."""
    count = 0
    if _redis_client:
        try:
            cursor = 0
            while True:
                cursor, keys = await _redis_client.scan(cursor, match=pattern, count=100)
                if keys:
                    await _redis_client.delete(*keys)
                    count += len(keys)
                if cursor == 0:
                    break
        except Exception:
            pass

    # In-memory fallback
    to_delete = [k for k in _memory_cache if _match_pattern(k, pattern)]
    for k in to_delete:
        del _memory_cache[k]
        count += 1
    return count


def _match_pattern(key: str, pattern: str) -> bool:
    """Simple glob pattern matching."""
    import fnmatch
    return fnmatch.fnmatch(key, pattern)


def _enforce_memory_limit():
    """Remove oldest entries if cache exceeds max size."""
    global _memory_cache
    if len(_memory_cache) >= _memory_max_size:
        # Remove 20% oldest entries
        sorted_items = sorted(_memory_cache.items(), key=lambda x: x[1][1])
        remove_count = max(1, _memory_max_size // 5)
        for k, _ in sorted_items[:remove_count]:
            del _memory_cache[k]


def cached(ttl: int = 300, key_prefix: str = ""):
    """Decorator to cache async function results."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Build cache key from function name and arguments
            key_parts = [key_prefix or func.__name__]
            key_parts.extend(str(a) for a in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)

            # Try cache
            cached_val = await cache_get(cache_key)
            if cached_val is not None:
                return cached_val

            # Call function and cache result
            result = await func(*args, **kwargs)
            await cache_set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator


# Cache key builders
def room_list_key(user_id: int) -> str:
    return f"rooms:user:{user_id}"


def room_messages_key(room_id: int, limit: int = 200) -> str:
    return f"messages:room:{room_id}:limit:{limit}"


def user_key(user_id: int) -> str:
    return f"user:{user_id}"


def user_by_token_key(token: str) -> str:
    return f"user:token:{token}"


def read_states_key(user_id: int, room_ids: tuple) -> str:
    return f"readstates:user:{user_id}:rooms:{','.join(map(str, sorted(room_ids)))}"


def room_read_state_key(room_id: int) -> str:
    return f"readstates:room:{room_id}"


async def invalidate_user_cache(user_id: int):
    """Invalidate all cache entries for a user."""
    await cache_delete_pattern(f"rooms:user:{user_id}")
    await cache_delete_pattern(f"readstates:user:{user_id}:*")
    await cache_delete(user_key(user_id))


async def invalidate_room_cache(room_id: int):
    """Invalidate all cache entries for a room."""
    await cache_delete_pattern(f"messages:room:{room_id}:*")
    await cache_delete_pattern(f"readstates:room:{room_id}")
    # Note: room lists per user are harder to invalidate selectively
    # Could use cache_delete_pattern("rooms:user:*") but that's broad