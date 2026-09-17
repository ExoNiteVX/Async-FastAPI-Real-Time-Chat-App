from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, ChatRoom, Message, RoomReadState, room_participants
from datetime import datetime, timezone

async def get_user_by_token(db: AsyncSession, token: str):
    res = await db.execute(select(User).where(User.token == token))
    return res.scalar_one_or_none()


async def check_room_participant(db: AsyncSession, room_id: int, user_id: int) -> bool:
    res = await db.execute(
        select(room_participants).where(
            room_participants.c.room_id == room_id,
            room_participants.c.user_id == user_id,
        )
    )
    return res.first() is not None


async def get_room(db: AsyncSession, room_id: int):
    res = await db.execute(
        select(ChatRoom)
        .options(selectinload(ChatRoom.participants))
        .where(ChatRoom.id == room_id)
    )
    return res.scalar_one_or_none()


async def list_rooms_for_user(db: AsyncSession, user_id: int):
    res = await db.execute(
        select(ChatRoom)
        .join(room_participants, room_participants.c.room_id == ChatRoom.id)
        .options(selectinload(ChatRoom.participants))
        .where(room_participants.c.user_id == user_id)
        .order_by(ChatRoom.id)
    )
    return res.scalars().all()


async def find_direct_room(db: AsyncSession, user_a: int, user_b: int):
    rooms = await list_rooms_for_user(db, user_a)
    for r in rooms:
        if r.type == "direct":
            ids = sorted(p.id for p in r.participants)
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
    return await get_room(db, room.id)


async def rename_room(db: AsyncSession, room_id: int, new_name: str):
    await db.execute(
        update(ChatRoom).where(ChatRoom.id == room_id).values(name=new_name)
    )
    await db.commit()
    return await get_room(db, room_id)

async def delete_room(db: AsyncSession, room_id: int):
    await db.execute(delete(RoomReadState).where(RoomReadState.room_id == room_id))
    await db.execute(delete(Message).where(Message.room_id == room_id))
    await db.execute(
        delete(room_participants).where(room_participants.c.room_id == room_id)
    )
    await db.execute(delete(ChatRoom).where(ChatRoom.id == room_id))
    await db.commit()


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


    res = await db.execute(select(User.username).where(User.id == sender_id))
    username = res.scalar() or "?"

    return {
        "id": msg.id,
        "room_id": msg.room_id,
        "sender_id": msg.sender_id,
        "sender": username,
        "text": msg.text,
        "files": list(msg.files or []),
        "timestamp": msg.timestamp,
    }


async def get_message(db: AsyncSession, message_id: int):
    res = await db.execute(select(Message).where(Message.id == message_id))
    return res.scalar_one_or_none()

async def get_messages(db: AsyncSession, room_id: int, limit: int = 200):
    res = await db.execute(
        select(Message)
        .options(selectinload(Message.sender))
        .where(Message.room_id == room_id)
        .order_by(Message.id.asc())
        .limit(limit)
    )
    return res.scalars().all()

async def update_message_text(db: AsyncSession, message_id: int, new_text: str) -> dict:
    msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one()
    msg.text = new_text
    msg.edited_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(msg)
    username = (
        await db.execute(select(User.username).where(User.id == msg.sender_id))
    ).scalar() or "?"
    return {
        "id": msg.id,
        "room_id": msg.room_id,
        "sender_id": msg.sender_id,
        "sender": username,
        "text": msg.text,
        "files": list(msg.files or []),
        "timestamp": msg.timestamp,
        "edited_at": msg.edited_at,
    }


async def delete_message(db: AsyncSession, message_id: int) -> None:
    await db.execute(delete(Message).where(Message.id == message_id))
    await db.commit()


async def get_read_states_for_room(db: AsyncSession, room_id: int) -> dict[int, int]:
    res = await db.execute(
        select(RoomReadState).where(RoomReadState.room_id == room_id)
    )
    return {rs.user_id: rs.last_read_message_id for rs in res.scalars().all()}


async def get_user_read_states_for_rooms(
    db: AsyncSession, user_id: int, room_ids: list[int]
) -> dict[int, int]:
    if not room_ids:
        return {}
    res = await db.execute(
        select(RoomReadState).where(
            RoomReadState.user_id == user_id,
            RoomReadState.room_id.in_(room_ids),
        )
    )
    return {rs.room_id: rs.last_read_message_id for rs in res.scalars().all()}


async def count_unread(
    db: AsyncSession, room_id: int, user_id: int, last_read_id: int
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.room_id == room_id,
            Message.id > last_read_id,
            Message.sender_id != user_id,
        )
    )
    return res.scalar() or 0


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
        return last_read_message_id
    if state.last_read_message_id >= last_read_message_id:
        return None
    state.last_read_message_id = last_read_message_id
    await db.commit()
    return last_read_message_id