from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, ChatRoom, Message, RoomReadState, room_participants
from datetime import datetime, timezone
from cache import (
    cache_get, cache_set, cache_delete, cache_delete_pattern,
    user_key, user_by_token_key, room_list_key, room_messages_key,
    read_states_key, room_read_state_key,
    invalidate_user_cache, invalidate_room_cache, wrap, AttrDict,
)
from auth import hash_password_async, verify_password_async, generate_token

async def get_user_by_token(db: AsyncSession, token: str):
    # Try cache first
    cache_key = user_by_token_key(token)
    cached = await cache_get(cache_key)
    if cached:
        # Return a lightweight object exposing the cached fields
        class CachedUser:
            def __init__(self, data):
                self.id = data["id"]
                self.username = data["username"]
                self.token = data["token"]
                self.email = data.get("email")
                self.display_name = data.get("display_name")
                self.avatar_url = data.get("avatar_url")
                self.bio = data.get("bio")
        return CachedUser(cached)

    res = await db.execute(select(User).where(User.token == token))
    user = res.scalar_one_or_none()
    if user:
        await cache_set(cache_key, {
            "id": user.id,
            "username": user.username,
            "token": user.token,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "bio": user.bio,
        }, ttl=600)
    return user


async def check_room_participant(db: AsyncSession, room_id: int, user_id: int) -> bool:
    res = await db.execute(
        select(room_participants).where(
            room_participants.c.room_id == room_id,
            room_participants.c.user_id == user_id,
        )
    )
    return res.first() is not None


async def get_room(db: AsyncSession, room_id: int):
    # Not caching individual rooms as they're less frequently accessed alone
    res = await db.execute(
        select(ChatRoom)
        .options(selectinload(ChatRoom.participants))
        .where(ChatRoom.id == room_id)
    )
    room = res.scalar_one_or_none()
    if room:
        return _room_to_dict(room)
    return None


def _room_to_dict(room: ChatRoom) -> AttrDict:
    """Convert ChatRoom SQLAlchemy object to dictionary for caching."""
    return wrap({
        "id": room.id,
        "type": room.type,
        "name": room.name,
        "owner_id": room.owner_id,
        "participants": [
            {
                "id": p.id,
                "username": p.username,
                "display_name": p.display_name,
                "avatar_url": p.avatar_url,
            }
            for p in room.participants
        ],
    })


async def list_rooms_for_user(db: AsyncSession, user_id: int):
    # Try cache first
    cache_key = room_list_key(user_id)
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    res = await db.execute(
        select(ChatRoom)
        .join(room_participants, room_participants.c.room_id == ChatRoom.id)
        .options(selectinload(ChatRoom.participants))
        .where(room_participants.c.user_id == user_id)
        .order_by(ChatRoom.id)
    )
    rooms = res.scalars().all()
    
    # Convert to dicts for caching
    room_dicts = [_room_to_dict(r) for r in rooms]
    await cache_set(cache_key, room_dicts, ttl=120)
    return room_dicts


async def find_direct_room(db: AsyncSession, user_a: int, user_b: int):
    # Use cached room list
    rooms = await list_rooms_for_user(db, user_a)
    for r in rooms:
        if r["type"] == "direct":
            ids = sorted(p["id"] for p in r["participants"])
            if ids == sorted([user_a, user_b]):
                return r
    return None


async def create_room(
    db: AsyncSession,
    type_: str,
    name,
    participant_ids,
    owner_id: int | None = None,
):
    room = ChatRoom(type=type_, name=name, owner_id=owner_id)
    res = await db.execute(select(User).where(User.id.in_(participant_ids)))
    room.participants = list(res.scalars().all())
    db.add(room)
    await db.commit()
    await db.refresh(room)
    
    # Invalidate room lists for all participants
    for pid in participant_ids:
        await invalidate_user_cache(pid)
    
    return await get_room(db, room.id)


async def rename_room(db: AsyncSession, room_id: int, new_name: str):
    await db.execute(
        update(ChatRoom).where(ChatRoom.id == room_id).values(name=new_name)
    )
    await db.commit()
    
    # Invalidate cache for this room
    await invalidate_room_cache(room_id)
    
    return await get_room(db, room_id)

async def delete_room(db: AsyncSession, room_id: int):
    # Get participant IDs before deletion for cache invalidation
    room = await get_room(db, room_id)
    participant_ids = [p.id for p in room.participants] if room else []
    
    await db.execute(delete(RoomReadState).where(RoomReadState.room_id == room_id))
    await db.execute(delete(Message).where(Message.room_id == room_id))
    await db.execute(
        delete(room_participants).where(room_participants.c.room_id == room_id)
    )
    await db.execute(delete(ChatRoom).where(ChatRoom.id == room_id))
    await db.commit()
    
    # Invalidate cache
    await invalidate_room_cache(room_id)
    for pid in participant_ids:
        await invalidate_user_cache(pid)


async def remove_participant(db: AsyncSession, room_id: int, user_id: int):
    await db.execute(
        delete(room_participants).where(
            room_participants.c.room_id == room_id,
            room_participants.c.user_id == user_id,
        )
    )
    await db.execute(
        delete(RoomReadState).where(
            RoomReadState.room_id == room_id,
            RoomReadState.user_id == user_id,
        )
    )
    await db.commit()
    
    # Invalidate cache
    await invalidate_room_cache(room_id)
    await invalidate_user_cache(user_id)


async def create_message(
    db: AsyncSession,
    room_id: int,
    sender_id: int,
    text: str | None = None,
    files: list[dict] | None = None,
) -> dict:

    msg = Message(
        room_id=room_id,
        sender_id=sender_id,
        text=text,
        files=files or [],
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)


    res = await db.execute(select(User.username, User.display_name).where(User.id == sender_id))
    row = res.first()
    username = row.username if row else "?"
    display_name = row.display_name if row and row.display_name else None

    # Invalidate message cache for this room
    await cache_delete_pattern(f"messages:room:{room_id}:*")

    return wrap({
        "id": msg.id,
        "room_id": msg.room_id,
        "sender_id": msg.sender_id,
        "sender": username,
        "sender_display_name": display_name,
        "text": msg.text,
        "files": list(msg.files or []),
        "timestamp": msg.timestamp,
    })


async def get_message(db: AsyncSession, message_id: int):
    res = await db.execute(select(Message).where(Message.id == message_id))
    return res.scalar_one_or_none()

def _message_to_dict(msg: Message) -> AttrDict:
    """Convert Message SQLAlchemy object to dictionary for caching."""
    sender_username = msg.sender.username if msg.sender else "?"
    sender_display_name = msg.sender.display_name if msg.sender else None
    return wrap({
        "id": msg.id,
        "room_id": msg.room_id,
        "sender_id": msg.sender_id,
        "sender": sender_username,
        "sender_display_name": sender_display_name,
        "text": msg.text,
        "files": list(msg.files or []),
        "edited_at": msg.edited_at,
        "timestamp": msg.timestamp,
    })


async def get_messages(db: AsyncSession, room_id: int, limit: int = 200):
    # Try cache first
    cache_key = room_messages_key(room_id, limit)
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    res = await db.execute(
        select(Message)
        .options(selectinload(Message.sender))
        .where(Message.room_id == room_id)
        .order_by(Message.id.asc())
        .limit(limit)
    )
    messages = res.scalars().all()
    
    # Convert to dicts for caching
    message_dicts = [_message_to_dict(m) for m in messages]
    await cache_set(cache_key, message_dicts, ttl=60)
    return message_dicts

async def update_message_text(db: AsyncSession, message_id: int, new_text: str) -> dict:
    msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one()
    msg.text = new_text
    msg.edited_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(msg)
    res = await db.execute(select(User.username, User.display_name).where(User.id == msg.sender_id))
    row = res.first()
    username = row.username if row else "?"
    display_name = row.display_name if row and row.display_name else None
    
    # Invalidate message cache for this room
    await cache_delete_pattern(f"messages:room:{msg.room_id}:*")
    
    return wrap({
        "id": msg.id,
        "room_id": msg.room_id,
        "sender_id": msg.sender_id,
        "sender": username,
        "sender_display_name": display_name,
        "text": msg.text,
        "files": list(msg.files or []),
        "timestamp": msg.timestamp,
        "edited_at": msg.edited_at,
    })


async def delete_message(db: AsyncSession, message_id: int) -> None:
    # Get room_id before deletion for cache invalidation
    msg = await db.execute(select(Message.room_id).where(Message.id == message_id))
    room_id = msg.scalar_one_or_none()
    
    await db.execute(delete(Message).where(Message.id == message_id))
    await db.commit()
    
    if room_id:
        await cache_delete_pattern(f"messages:room:{room_id}:*")


async def get_read_states_for_room(db: AsyncSession, room_id: int) -> dict[int, int]:
    # Try cache first
    cache_key = room_read_state_key(room_id)
    cached = await cache_get(cache_key)
    if cached is not None:
        # JSON round-trips integer keys as strings; normalize back to ints.
        return {int(k): int(v) for k, v in cached.items()}

    res = await db.execute(
        select(RoomReadState).where(RoomReadState.room_id == room_id)
    )
    result = {rs.user_id: rs.last_read_message_id for rs in res.scalars().all()}
    await cache_set(cache_key, result, ttl=120)
    return result


async def get_user_read_states_for_rooms(
    db: AsyncSession, user_id: int, room_ids: list[int]
) -> dict[int, int]:
    if not room_ids:
        return {}
    
    # Try cache first
    cache_key = read_states_key(user_id, tuple(room_ids))
    cached = await cache_get(cache_key)
    if cached is not None:
        # Normalize string keys produced by the JSON cache back to ints.
        return {int(k): int(v) for k, v in cached.items()}

    res = await db.execute(
        select(RoomReadState).where(
            RoomReadState.user_id == user_id,
            RoomReadState.room_id.in_(room_ids),
        )
    )
    result = {rs.room_id: rs.last_read_message_id for rs in res.scalars().all()}
    await cache_set(cache_key, result, ttl=120)
    return result


async def count_unread(
    db: AsyncSession, room_id: int, user_id: int, last_read_id: int
) -> int:
    # Try cache first
    cache_key = f"unread:room:{room_id}:user:{user_id}:last:{last_read_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    res = await db.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.room_id == room_id,
            Message.id > last_read_id,
            Message.sender_id != user_id,
        )
    )
    result = res.scalar() or 0
    await cache_set(cache_key, result, ttl=60)
    return result


async def mark_room_read(
    db: AsyncSession, room_id: int, user_id: int, last_read_message_id: int
) -> int | None:
    res = await db.execute(
        select(RoomReadState).where(
            RoomReadState.user_id == user_id,
            RoomReadState.room_id == room_id,
        )
    )
    state = res.scalar_one_or_none()
    if state is None:
        db.add(RoomReadState(
            user_id=user_id, room_id=room_id,
            last_read_message_id=last_read_message_id,
        ))
        await db.commit()
        
        # Invalidate read state caches
        await cache_delete(room_read_state_key(room_id))
        await cache_delete_pattern(f"readstates:user:{user_id}:*")
        await cache_delete_pattern(f"unread:room:{room_id}:user:{user_id}:*")
        
        return last_read_message_id
    if state.last_read_message_id >= last_read_message_id:
        return None
    state.last_read_message_id = last_read_message_id
    await db.commit()
    
    # Invalidate read state caches
    await cache_delete(room_read_state_key(room_id))
    await cache_delete_pattern(f"readstates:user:{user_id}:*")
    await cache_delete_pattern(f"unread:room:{room_id}:user:{user_id}:*")
    
    return last_read_message_id


# ============================================================================
# Authentication functions
# ============================================================================

async def get_user_by_email(db: AsyncSession, email: str):
    """Get user by email address."""
    res = await db.execute(select(User).where(User.email == email))
    return res.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str):
    """Get user by username."""
    res = await db.execute(select(User).where(User.username == username))
    return res.scalar_one_or_none()


async def register_user(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
) -> User:
    """
    Register a new user.
    Hashes the password and generates an auth token.
    """
    # Hash password using bcrypt (runs in thread pool)
    password_hash = await hash_password_async(password)
    
    # Generate secure token for API authentication
    token = generate_token()
    
    # Create user
    user = User(
        username=username,
        email=email,
        password_hash=password_hash,
        token=token,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    # Invalidate user cache
    await invalidate_user_cache(user.id)
    
    return user


async def authenticate_user(
    db: AsyncSession,
    email: str,
    password: str,
) -> User | None:
    """
    Authenticate a user by email and password.
    Uses constant-time verification to prevent timing attacks.
    """
    from auth import verify_password_async, simulate_timing_attack_prevention
    
    # Get user by email
    user = await get_user_by_email(db, email)
    
    if user is None:
        # User doesn't exist - still run verification to prevent timing attacks
        # Use a dummy hash to simulate the same computation time
        await verify_password_async(password, "$2b$12$dummyhashdummyhashdummyhashdummyhashdummy")
        await simulate_timing_attack_prevention()
        return None
    
    # Verify password (runs in thread pool)
    is_valid = await verify_password_async(password, user.password_hash)
    
    if not is_valid:
        # Invalid password - still add delay to prevent timing attacks
        await simulate_timing_attack_prevention()
        return None
    
    # Password is valid, return user
    return user


async def update_user_token(db: AsyncSession, user_id: int) -> str:
    """Generate and update user's auth token."""
    from auth import generate_token
    token = generate_token()
    await db.execute(
        update(User).where(User.id == user_id).values(token=token)
    )
    await db.commit()
    
    # Invalidate cache
    await cache_delete_pattern(f"user:token:*")
    await invalidate_user_cache(user_id)
    
    return token


# ============================================================================
# User Profile & Search functions
# ============================================================================

async def search_users(db: AsyncSession, query: str, current_user_id: int, limit: int = 20):
    """
    Search users by username (case-insensitive partial match).
    Excludes the current user from results.
    """
    res = await db.execute(
        select(User)
        .where(
            User.username.ilike(f"%{query}%"),
            User.id != current_user_id,
        )
        .limit(limit)
    )
    return res.scalars().all()


async def get_user_by_id(db: AsyncSession, user_id: int):
    """
    Get a user's profile data by ID.

    Caches a plain, JSON-serializable record (never a raw ORM object, which
    cannot round-trip through Redis) and returns it as an AttrDict so callers
    can use attribute access such as ``user.display_name``.
    """
    cache_key = f"user:profile:{user_id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalar_one_or_none()
    if not user:
        return None

    data = wrap({
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "bio": user.bio,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    })
    await cache_set(cache_key, data, ttl=300)
    return data


async def get_user_public_profile(db: AsyncSession, user_id: int):
    """Get public profile info for a user (no sensitive data)."""
    user = await get_user_by_id(db, user_id)
    if not user:
        return None
    created_at = user.created_at
    if created_at is not None and not isinstance(created_at, str):
        created_at = created_at.isoformat()
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "bio": user.bio,
        "avatar_url": user.avatar_url,
        "created_at": created_at,
    }


async def update_user_profile(
    db: AsyncSession,
    user_id: int,
    display_name: str | None = None,
    bio: str | None = None,
    avatar_url: str | None = None,
) -> User | None:
    """Update user profile fields."""
    values = {}
    if display_name is not None:
        values["display_name"] = display_name.strip() or None
    if bio is not None:
        values["bio"] = bio.strip() or None
    if avatar_url is not None:
        values["avatar_url"] = avatar_url.strip() or None
    
    if not values:
        return await get_user_by_id(db, user_id)
    
    await db.execute(update(User).where(User.id == user_id).values(**values))
    await db.commit()
    
    # Invalidate cache.  Profile data appears in other users' cached room
    # lists (display_name / avatar_url), so clear those as well.
    await cache_delete(f"user:profile:{user_id}")
    await cache_delete_pattern("rooms:user:*")
    await cache_delete_pattern("user:token:*")
    await invalidate_user_cache(user_id)
    
    return await get_user_by_id(db, user_id)