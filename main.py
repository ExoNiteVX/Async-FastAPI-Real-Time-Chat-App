from datetime import timezone, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect, Depends,
    HTTPException, status, File, UploadFile, Header, Form,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from connection_manager import manager, user_manager
from database import AsyncSessionLocal, engine, Base, get_db, init_cache, close_cache
import crud
import models
import json
import uuid
import traceback
from cache import invalidate_room_cache, invalidate_user_cache
from auth import validate_email_address, validate_password_strength, validate_username

MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)

MAX_FILES_PER_MESSAGE = 5
DISPLAY_TZ = ZoneInfo("Asia/Tashkent")

app = FastAPI()
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


@app.on_event("startup")
async def _startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await init_cache()


@app.on_event("shutdown")
async def _shutdown():
    await close_cache()
    from auth import shutdown_executor
    shutdown_executor()


@app.get("/")
async def root():
    return FileResponse("index.html")


# ============================================================================
# Authentication Pydantic Models
# ============================================================================

class RegisterRequest(BaseModel):
    """Registration request model with validation."""
    username: str
    email: EmailStr
    password: str
    
    @field_validator("username")
    @classmethod
    def validate_username_field(cls, v: str) -> str:
        is_valid, error = validate_username(v)
        if not is_valid:
            raise ValueError(error)
        return v
    
    @field_validator("password")
    @classmethod
    def validate_password_field(cls, v: str) -> str:
        is_valid, error = validate_password_strength(v)
        if not is_valid:
            raise ValueError(error)
        return v


class LoginRequest(BaseModel):
    """Login request model."""
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    """Authentication response model."""
    user: dict
    token: str


async def get_current_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.split(" ", 1)[1]
    user = await crud.get_user_by_token(db, token)
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


# ============================================================================
# Authentication Endpoints
# ============================================================================

@app.post("/api/auth/register", response_model=AuthResponse)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new user.
    
    Validates:
    - Email format and uniqueness
    - Username format and uniqueness
    - Password strength (min 8 chars, uppercase, lowercase, digit, special char)
    
    Returns user info and auth token on success.
    """
    # Check if username already exists
    existing_user = await crud.get_user_by_username(db, payload.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken"
        )
    
    # Check if email already exists
    existing_email = await crud.get_user_by_email(db, payload.email)
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered"
        )
    
    # Register user (password hashing happens in crud)
    user = await crud.register_user(
        db,
        username=payload.username,
        email=payload.email,
        password=payload.password,
    )
    
    return AuthResponse(
        user={
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        token=user.token,
    )


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate a user with email and password.
    
    Implements timing attack prevention:
    - Always runs password verification (even for non-existent users)x
    - Adds constant-time delay (~0.7s) before returning error
    - Uses thread pool for bcrypt operations to avoid blocking event loop
    
    Returns user info and auth token on success.
    """
    # Authenticate user (includes timing attack prevention)
    user = await crud.authenticate_user(db, payload.email, payload.password)
    
    if not user:
        # Generic error message to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )
    
    return AuthResponse(
        user={
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        token=user.token,
    )


@app.post("/api/auth/refresh-token")
async def refresh_token(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a new auth token for the current user.
    Invalidates the old token.
    """
    new_token = await crud.update_user_token(db, user.id)
    return {"token": new_token}


# ============================================================================
# User Profile & Search Endpoints
# ============================================================================

class UpdateProfileRequest(BaseModel):
    """Profile update request model."""
    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    
    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) > 50:
                raise ValueError("Display name must be 50 characters or less")
        return v
    
    @field_validator("bio")
    @classmethod
    def validate_bio(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 500:
            raise ValueError("Bio must be 500 characters or less")
        return v


@app.get("/api/users/search")
async def search_users(
    q: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Search users by username (case-insensitive partial match).
    Returns users excluding the current user.
    """
    if len(q.strip()) < 2:
        return []
    
    users = await crud.search_users(db, q.strip(), user.id, limit=20)
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or u.username,
            "avatar_url": u.avatar_url,
        }
        for u in users
    ]


@app.get("/api/users/{user_id}/profile")
async def get_user_profile(
    user_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get public profile of a user.
    """
    profile = await crud.get_user_public_profile(db, user_id)
    if not profile:
        raise HTTPException(404, "User not found")
    return profile


@app.get("/api/me/profile")
async def get_my_profile(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current user's full profile (includes email).
    """
    db_user = await crud.get_user_by_id(db, user.id)
    if not db_user:
        raise HTTPException(404, "User not found")
    return {
        "id": db_user.id,
        "username": db_user.username,
        "email": db_user.email,
        "display_name": db_user.display_name or db_user.username,
        "bio": db_user.bio,
        "avatar_url": db_user.avatar_url,
        "created_at": _created_iso(db_user.created_at),
    }


@app.patch("/api/me/profile")
async def update_my_profile(
    payload: UpdateProfileRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update current user's profile.
    """
    updated = await crud.update_user_profile(
        db,
        user.id,
        display_name=payload.display_name,
        bio=payload.bio,
        avatar_url=payload.avatar_url,
    )
    if not updated:
        raise HTTPException(404, "User not found")
    
    return {
        "id": updated.id,
        "username": updated.username,
        "email": updated.email,
        "display_name": updated.display_name or updated.username,
        "bio": updated.bio,
        "avatar_url": updated.avatar_url,
        "created_at": _created_iso(updated.created_at),
    }


def _created_iso(value):
    """Return an ISO-8601 string for a datetime (or pass through a string)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _to_local_iso(dt):
    # Accept both datetime objects (fresh from the DB) and ISO/JSON strings
    # (round-tripped through the cache).
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).isoformat()


def _field(obj, name, default=None):
    """Read a field from either an AttrDict/dict or an ORM object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _serialize_room(room, current_user_id: int, unread: int = 0):
    # Works with both cached AttrDicts and live ORM objects.
    room_id = _field(room, "id")
    room_type = _field(room, "type")
    room_name = _field(room, "name")
    room_owner_id = _field(room, "owner_id")
    participants = _field(room, "participants", []) or []

    others = [p for p in participants if _field(p, "id") != current_user_id]
    if room_type == "direct":
        other = others[0] if others else None
        if other:
            display = _field(other, "display_name") or _field(other, "username")
        else:
            display = "(empty)"
    else:
        display = room_name or f"Group #{room_id}"

    serialized_participants = [
        {
            "id": _field(p, "id"),
            "username": _field(p, "username"),
            "display_name": _field(p, "display_name"),
            "avatar_url": _field(p, "avatar_url"),
        }
        for p in participants
    ]

    return {
        "id": room_id,
        "type": room_type,
        "name": room_name,
        "display": display,
        "owner_id": room_owner_id,
        "participants": serialized_participants,
        "unread": unread,
    }

def _serialize_message(m, read_states: dict[int, int] | None = None):
    read_states = read_states or {}

    if isinstance(m, dict):
        mid = m["id"]
        sender_id = m["sender_id"]
        sender_name = m.get("sender", "?")
        sender_display_name = m.get("sender_display_name")
        text = m.get("text")
        files = list(m.get("files") or [])
        ts = m.get("timestamp")
        edited_at = m.get("edited_at")
        room_id = m.get("room_id")
    else:
        mid = m.id
        sender_id = m.sender_id
        sender_name = "?"
        sender_display_name = None
        try:
            if m.sender is not None:
                sender_name = m.sender.username
                sender_display_name = m.sender.display_name
        except Exception:
            pass
        text = m.text
        files = list(m.files or [])
        ts = m.timestamp
        edited_at = getattr(m, "edited_at", None)
        room_id = m.room_id

    read_by = [
        uid for uid, last_id in read_states.items()
        if uid != sender_id and last_id >= mid
    ]
    return {
        "type": "message",
        "id": mid,
        "room_id": room_id,
        "sender": sender_name,
        "sender_display_name": sender_display_name,
        "sender_id": sender_id,
        "message": text,
        "files": files,
        "timestamp": _to_local_iso(ts),
        "edited_at": _to_local_iso(edited_at),
        "read_by": read_by,
    }

async def _notify_participants_new_message(db, room_id: int, msg_payload: dict):
    room = await crud.get_room(db, room_id)
    if not room:
        return
    ids = [p.id for p in room.participants]
    await user_manager.send_to_users(ids, {
        "type": "new_message",
        "room_id": room_id,
        "message": msg_payload,
    })

async def _notify_participants_event(db, room_id: int, event: dict):
    room = await crud.get_room(db, room_id)
    if not room:
        return
    ids = [p.id for p in room.participants]
    await user_manager.send_to_users(ids, event)

class CreateRoomIn(BaseModel):
    type: str = "direct"
    name: str | None = None
    participant_ids: list[int] = []


class UpdateRoomIn(BaseModel):
    name: str

class MarkReadIn(BaseModel):
    last_read_message_id: int


@app.post("/api/rooms/{room_id}/read")
async def mark_read_rest(
    room_id: int,
    payload: MarkReadIn,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    changed = await crud.mark_room_read(db, room_id, user.id, payload.last_read_message_id)
    if changed is None:
        return {"ok": True, "changed": False}
    # Tell the room so ticks update live
    await manager.broadcast_to_room(room_id, {
        "type": "read_receipt",
        "room_id": room_id,
        "reader_id": user.id,
        "reader_username": user.username,
        "last_read_message_id": changed,
    })
    return {"ok": True, "changed": True, "last_read_message_id": changed}


@app.get("/api/me")
async def me(user=Depends(get_current_user)):
    return {"id": user.id, "username": user.username}


@app.get("/api/users")
async def list_users(user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(models.User).order_by(models.User.username))
    return [
        {"id": u.id, "username": u.username}
        for u in res.scalars().all() if u.id != user.id
    ]


@app.get("/api/rooms")
async def list_rooms(user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rooms = await crud.list_rooms_for_user(db, user.id)
    room_ids = [r["id"] for r in rooms]
    read_states = await crud.get_user_read_states_for_rooms(
        db, user.id, room_ids
    )
    out = []
    for r in rooms:
        last_read = read_states.get(r["id"], 0)
        unread = await crud.count_unread(db, r["id"], user.id, last_read)
        out.append(_serialize_room(r, user.id, unread))
    return out


@app.post("/api/rooms")
async def create_room(
    payload: CreateRoomIn,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.type == "direct":
        if len(payload.participant_ids) != 1:
            raise HTTPException(400, "Direct room needs exactly one other participant")
        other_id = payload.participant_ids[0]
        existing = await crud.find_direct_room(db, user.id, other_id)
        if existing:
            return _serialize_room(existing, user.id)
        room = await crud.create_room(db, "direct", None, [user.id, other_id])
    elif payload.type == "group":
        ids = list({user.id, *payload.participant_ids})
        if len(ids) < 2:
            raise HTTPException(400, "Group needs at least 2 participants")
        room = await crud.create_room(
            db, "group", payload.name or "New group", ids, owner_id=user.id
        )
    else:
        raise HTTPException(400, "type must be 'direct' or 'group'")

    # room is now a dict from crud functions
    participants = room.get("participants", [])
    for p in participants:
        if p["id"] == user.id:
            continue
        await user_manager.send_to_user(p["id"], {
            "type": "new_room",
            "room": _serialize_room(room, p["id"], 0),
        })

    return _serialize_room(room, user.id)


@app.patch("/api/rooms/{room_id}")
async def update_room(
    room_id: int,
    payload: UpdateRoomIn,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    room = await crud.get_room(db, room_id)
    if room.get("type") != "group":
        raise HTTPException(400, "Only groups can be renamed")
    if room.get("owner_id") != user.id:
        raise HTTPException(403, "Only the group owner can rename this group")
    new_name = (payload.name or "").strip()
    if not new_name:
        raise HTTPException(400, "Name cannot be empty")
    room = await crud.rename_room(db, room_id, new_name)

    participants = room.get("participants", [])
    for p in participants:
        await user_manager.send_to_user(p["id"], {
            "type": "room_updated",
            "room_id": room.get("id"),
            "name": new_name,
            "display": new_name,
        })

    return _serialize_room(room, user.id)


class EditMessageIn(BaseModel):
    text: str


@app.patch("/api/rooms/{room_id}/messages/{message_id}")
async def edit_message_route(
    room_id: int,
    message_id: int,
    payload: EditMessageIn,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    msg = await crud.get_message(db, message_id)
    if msg is None or msg.room_id != room_id:
        raise HTTPException(404, "Message not found")
    if msg.sender_id != user.id:
        raise HTTPException(403, "You can only edit your own messages")

    new_text = (payload.text or "").strip()
    if not new_text:
        raise HTTPException(400, "Message text cannot be empty")

    data = await crud.update_message_text(db, message_id, new_text)
    out = _serialize_message(data)

    await manager.broadcast_to_room(room_id, {
        "type": "message_edited",
        "room_id": room_id,
        "message": out,
    })
    try:
        async with AsyncSessionLocal() as db2:
            await _notify_participants_event(db2, room_id, {
                "type": "message_edited",
                "room_id": room_id,
                "message": out,
            })
    except Exception:
        pass
    return out


@app.delete("/api/rooms/{room_id}/messages/{message_id}")
async def delete_message_route(
    room_id: int,
    message_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    msg = await crud.get_message(db, message_id)
    if msg is None or msg.room_id != room_id:
        raise HTTPException(404, "Message not found")
    if msg.sender_id != user.id:
        raise HTTPException(403, "You can only delete your own messages")

    await crud.delete_message(db, message_id)

    await manager.broadcast_to_room(room_id, {
        "type": "message_deleted",
        "room_id": room_id,
        "message_id": message_id,
    })
    try:
        async with AsyncSessionLocal() as db2:
            await _notify_participants_event(db2, room_id, {
                "type": "message_deleted",
                "room_id": room_id,
                "message_id": message_id,
            })
    except Exception:
        pass
    return {"status": "ok"}


@app.delete("/api/rooms/{room_id}")
async def delete_room_endpoint(
    room_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    room = await crud.get_room(db, room_id)
    if room.type != "group":
        raise HTTPException(400, "Only groups can be deleted")
    if room.owner_id != user.id:
        raise HTTPException(403, "Only the group owner can delete this group")

    participant_ids = [p.id for p in room.participants]
    await crud.delete_room(db, room_id)

    for uid in participant_ids:
        await user_manager.send_to_user(uid, {
            "type": "room_deleted",
            "room_id": room_id,
        })
    return {"status": "ok"}


@app.post("/api/rooms/{room_id}/leave")
async def leave_room_endpoint(
    room_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    room = await crud.get_room(db, room_id)
    if room.type != "group":
        raise HTTPException(400, "Direct rooms cannot be left")
    if room.owner_id == user.id:
        raise HTTPException(400, "Owner must delete the group instead of leaving")

    participant_ids = [p.id for p in room.participants]
    await crud.remove_participant(db, room_id, user.id)

    await user_manager.send_to_user(user.id, {
        "type": "room_removed",
        "room_id": room_id,
    })
    for uid in participant_ids:
        if uid == user.id:
            continue
        await user_manager.send_to_user(uid, {
            "type": "participant_left",
            "room_id": room_id,
            "user_id": user.id,
            "username": user.username,
        })
    return {"status": "ok"}

@app.get("/api/rooms/{room_id}/messages")
async def room_messages(
    room_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, user.id):
        raise HTTPException(403, "Not a participant")
    msgs = await crud.get_messages(db, room_id)
    read_states = await crud.get_read_states_for_room(db, room_id)
    return [_serialize_message(m, read_states) for m in msgs]


@app.post("/api/chat/{room_id}/upload")
async def upload_files(
    room_id: int,
    files: list[UploadFile] = File(default=[]),
    text: str = Form(None),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await crud.check_room_participant(db, room_id, current_user.id):
        raise HTTPException(403, "Not a participant")

    text = (text or "").strip() or None
    files = [f for f in files if f and f.filename]

    if not files and not text:
        raise HTTPException(400, "Nothing to send")
    if len(files) > MAX_FILES_PER_MESSAGE:
        raise HTTPException(
            400, f"Max {MAX_FILES_PER_MESSAGE} files per message (got {len(files)})"
        )

    saved = []
    for f in files:
        original = f.filename
        ext = Path(original).suffix.lower()
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = MEDIA_DIR / stored
        size = 0
        with dest.open("wb") as fh:
            while chunk := await f.read(1024 * 1024):
                fh.write(chunk)
                size += len(chunk)
        saved.append({"url": f"/media/{stored}", "name": original, "size": size})

    msg = await crud.create_message(
        db, room_id=room_id, sender_id=current_user.id, text=text, files=saved
    )
    payload = _serialize_message(msg)
    await manager.broadcast_to_room(room_id, payload)
    await _notify_participants_new_message(db, room_id, payload)
    return {"status": "success", "message": payload}


@app.websocket("/ws/chat/{room_id}")
async def websocket_dm_endpoint(websocket: WebSocket, room_id: int, token: str):
    try:
        async with AsyncSessionLocal() as db:
            user = await crud.get_user_by_token(db, token)
            if not user:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if not await crud.check_room_participant(db, room_id, user.id):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            user_id = user.id
            username = user.username
    except Exception:
        print("[WS auth] failed:")
        traceback.print_exc()
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await manager.connect(websocket, room_id)
    print(f"[WS +] user={username} room={room_id}")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[WS !] non-json from user={username}: {raw[:120]!r}")
                continue

            kind = data.get("type")
            print(f"[WS ←] user={username} room={room_id} kind={kind}")

            if kind == "chat_message":
                text = (data.get("message") or "").strip()
                if not text:
                    continue
                try:
                    async with AsyncSessionLocal() as db:
                        msg_dict = await crud.create_message(db, room_id, user_id, text=text)
                    payload = _serialize_message(msg_dict)

                    await manager.broadcast_to_room(room_id, payload)
                    print(f"[WS →] broadcast id={payload['id']} to room={room_id}")

                    try:
                        async with AsyncSessionLocal() as db:
                            await _notify_participants_new_message(db, room_id, payload)
                    except Exception:
                        print("[WS notify] failed:")
                        traceback.print_exc()
                except Exception:
                    print(f"[WS chat_message] FAILED user={username} room={room_id}:")
                    traceback.print_exc()

            elif kind == "mark_read":
                try:
                    last_id = int(data.get("last_read_message_id") or 0)
                except (TypeError, ValueError):
                    continue
                if last_id <= 0:
                    continue
                try:
                    async with AsyncSessionLocal() as db:
                        changed = await crud.mark_room_read(db, room_id, user_id, last_id)
                    if changed is not None:
                        await manager.broadcast_to_room(room_id, {
                            "type": "read_receipt",
                            "room_id": room_id,
                            "reader_id": user_id,
                            "reader_username": username,
                            "last_read_message_id": changed,
                        })
                except Exception:
                    print(f"[WS mark_read] FAILED user={username} room={room_id}:")
                    traceback.print_exc()

    except WebSocketDisconnect:
        print(f"[WS -] user={username} room={room_id}")
    except Exception:
        print(f"[WS outer] FAILED user={username} room={room_id}:")
        traceback.print_exc()
    finally:
        manager.disconnect(websocket, room_id)

@app.websocket("/ws/user")
async def websocket_user_endpoint(websocket: WebSocket, token: str):
    async with AsyncSessionLocal() as db:
        user = await crud.get_user_by_token(db, token)
        if not user:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        user_id = user.id

    await user_manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        print("[WS user outer] failed:")
        traceback.print_exc()
    finally:
        user_manager.disconnect(websocket, user_id)