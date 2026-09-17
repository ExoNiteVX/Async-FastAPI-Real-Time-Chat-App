from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Table, JSON
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base

room_participants = Table(
    "room_participants",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("room_id", Integer, ForeignKey("chat_rooms.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    token = Column(String, unique=True, index=True)

class ChatRoom(Base):
    __tablename__ = "chat_rooms"
    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, default="direct")
    name = Column(String, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    participants = relationship("User", secondary=room_participants)
    messages = relationship("Message", back_populates="room", order_by="Message.id")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("chat_rooms.id"))
    sender_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String, nullable=True)
    files = Column(JSON, default=list)
    edited_at = Column(DateTime, nullable=True)
    timestamp = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    room = relationship("ChatRoom", back_populates="messages")
    sender = relationship("User")


class RoomReadState(Base):
    __tablename__ = "room_read_states"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    room_id = Column(Integer, ForeignKey("chat_rooms.id"), primary_key=True)
    last_read_message_id = Column(Integer, default=0)