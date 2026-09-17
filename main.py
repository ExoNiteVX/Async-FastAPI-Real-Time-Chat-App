from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect, Depends,
    HTTPException, status, File, UploadFile, Header, Form,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from connection_manager import manager, user_manager
from database import AsyncSessionLocal, engine, Base, get_db
import crud
import models
import json
import uuid
import traceback

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


@app.get("/")
async def root():
    return FileResponse("index.html")


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


def _to_local_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).isoformat()


def _serialize_room(room, current_user_id: int, unread: int = 0):
    others = [p for p in room.participants if p.id != current_user_id]
    if room.type == "direct":
        display = others[0].username if others else "(empty)"
    else:
        display = room.name or f"Group #{room.id}"
    return {
        "id": room.id,
        "type": room.type,
        "name": room.name,
        "display": display,
        "owner_id": room.owner_id,
        "participants": [{"id": p.id, "username": p.username} for p in room.participants],
        "unread": unread,
    }

def _serialize_message(m, read_states: dict[int, int] | None = None):
    read_states = read_states or {}

    if isinstance(m, dict):
        mid = m["id"]
        sender_id = m["sender_id"]
        sender_name = m.get("sender", "?")
        text = m.get("text")
        files = list(m.get("files") or [])
        ts = m.get("timestamp")
        edited_at = m.get("edited_at")
        room_id = m.get("room_id")
    else:
        mid = m.id
        sender_id = m.sender_id
        sender_name = "?"
        try:
            if m.sender is not None:
                sender_name = m.sender.username
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
    read_states = await crud.get_user_read_states_for_rooms(
        db, user.id, [r.id for r in rooms]
    )
    out = []
    for r in rooms:
        last_read = read_states.get(r.id, 0)
        unread = await crud.count_unread(db, r.id, user.id, last_read)
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

    for p in room.participants:
        if p.id == user.id:
            continue
        await user_manager.send_to_user(p.id, {
            "type": "new_room",
            "room": _serialize_room(room, p.id, 0),
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
    if room.type != "group":
        raise HTTPException(400, "Only groups can be renamed")
    if room.owner_id != user.id:
        raise HTTPException(403, "Only the group owner can rename this group")
    new_name = (payload.name or "").strip()
    if not new_name:
        raise HTTPException(400, "Name cannot be empty")
    room = await crud.rename_room(db, room_id, new_name)

    for p in room.participants:
        await user_manager.send_to_user(p.id, {
            "type": "room_updated",
            "room_id": room.id,
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