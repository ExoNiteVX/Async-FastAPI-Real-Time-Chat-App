import json
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: int):
        await websocket.accept()
        self.active_connections.setdefault(room_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, room_id: int):
        conns = self.active_connections.get(room_id)
        if not conns:
            return
        try:
            conns.remove(websocket)
        except ValueError:
            pass
        if not conns:
            self.active_connections.pop(room_id, None)

    async def broadcast_to_room(self, room_id: int, message_data: dict):
        conns = list(self.active_connections.get(room_id, []))
        dead = []
        for connection in conns:
            try:
                await connection.send_text(json.dumps(message_data, default=str))
            except Exception:
                dead.append(connection)
        for d in dead:
            self.disconnect(d, room_id)


class UserConnectionManager:
    def __init__(self):
        self.connections: dict[int, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: int):
        conns = self.connections.get(user_id)
        if not conns:
            return
        try:
            conns.remove(websocket)
        except ValueError:
            pass
        if not conns:
            self.connections.pop(user_id, None)

    async def send_to_user(self, user_id: int, payload: dict):
        conns = list(self.connections.get(user_id, []))
        dead = []
        for connection in conns:
            try:
                await connection.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(connection)
        for d in dead:
            self.disconnect(d, user_id)

    async def send_to_users(self, user_ids: list[int], payload: dict):
        for uid in user_ids:
            await self.send_to_user(uid, payload)


manager = ConnectionManager()
user_manager = UserConnectionManager()