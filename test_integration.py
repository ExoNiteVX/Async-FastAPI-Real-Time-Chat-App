"""
End-to-end integration test for the chat app.

It drives a running server over HTTP and WebSocket and exercises every
endpoint that reads or writes cached data (rooms, messages, profiles, read
states). This is what catches "dict vs ORM object" regressions such as:

    AttributeError: 'dict' object has no attribute 'participants'

Usage:
    1. (re)seed the database:        python seed.py
    2. start the server:             uvicorn main:app --port 8000
    3. run the test (in another shell):
       python test_integration.py http://127.0.0.1:8000

By default the app uses Redis (REDIS_URL) and falls back to an in-memory
cache when Redis is unavailable; both paths are covered by this test.
"""
import asyncio
import io
import json
import sys
import uuid

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
WS = BASE.replace("http", "ws")

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def login(client, email, password):
    r = await client.post(f"{BASE}/api/auth/login",
                          json={"email": email, "password": password})
    check(f"login {email}", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    return r.json()["token"]


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        # --- register a fresh user (unique per run) ---
        uname = f"tester_{uuid.uuid4().hex[:8]}"
        email = f"{uname}@example.com"
        r = await client.post(f"{BASE}/api/auth/register", json={
            "username": uname, "email": email, "password": "StrongPass1!",
        })
        check("register", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        alice = await login(client, "alice@example.com", "DemoPass123!")
        bob = await login(client, "bob@example.com", "DemoPass123!")

        # --- current user ---
        r = await client.get(f"{BASE}/api/me", headers=auth(alice))
        check("GET /api/me", r.status_code == 200, r.text[:200])

        # --- user search ---
        r = await client.get(f"{BASE}/api/users/search?q=bo", headers=auth(alice))
        check("search users",
              r.status_code == 200 and any(u["username"] == "bob" for u in r.json()),
              r.text[:200])
        bob_id = next(u["id"] for u in r.json() if u["username"] == "bob")

        # --- public profile (cold DB hit, then cached hit) ---
        r = await client.get(f"{BASE}/api/users/{bob_id}/profile", headers=auth(alice))
        check("get public profile (cold)", r.status_code == 200, r.text[:200])
        r2 = await client.get(f"{BASE}/api/users/{bob_id}/profile", headers=auth(alice))
        check("get public profile (cached)",
              r2.status_code == 200 and r2.json() == r.json(), r2.text[:200])

        # --- my profile (cached path + created_at handling) ---
        r = await client.get(f"{BASE}/api/me/profile", headers=auth(alice))
        check("GET /api/me/profile (cold)", r.status_code == 200, r.text[:200])
        r2 = await client.get(f"{BASE}/api/me/profile", headers=auth(alice))
        check("GET /api/me/profile (cached)",
              r2.status_code == 200 and r2.json().get("id") == r.json().get("id"),
              r2.text[:200])

        # --- update profile ---
        r = await client.patch(f"{BASE}/api/me/profile", headers=auth(alice),
                               json={"display_name": "Alice C", "bio": "hi",
                                     "avatar_url": "https://x/y.png"})
        check("PATCH /api/me/profile",
              r.status_code == 200 and r.json()["display_name"] == "Alice C",
              r.text[:200])

        # --- create DM (find_direct_room reads the cached room list) ---
        r = await client.post(f"{BASE}/api/rooms", headers=auth(alice),
                              json={"type": "direct", "participant_ids": [bob_id]})
        check("create direct room", r.status_code == 200, r.text[:200])
        dm_id = r.json()["id"]

        r2 = await client.post(f"{BASE}/api/rooms", headers=auth(alice),
                               json={"type": "direct", "participant_ids": [bob_id]})
        check("create direct room idempotent",
              r2.status_code == 200 and r2.json()["id"] == dm_id, r2.text[:200])

        # --- list rooms (cold + cached) ---
        r = await client.get(f"{BASE}/api/rooms", headers=auth(alice))
        check("list rooms (cold)", r.status_code == 200, r.text[:200])
        r2 = await client.get(f"{BASE}/api/rooms", headers=auth(alice))
        check("list rooms (cached)", r2.status_code == 200, r2.text[:200])

        # --- messages (cold + cached) ---
        r = await client.get(f"{BASE}/api/rooms/{dm_id}/messages", headers=auth(alice))
        check("get messages (cold)", r.status_code == 200, r.text[:200])
        r2 = await client.get(f"{BASE}/api/rooms/{dm_id}/messages", headers=auth(alice))
        check("get messages (cached)", r2.status_code == 200, r2.text[:200])

        # --- create group ---
        r = await client.post(f"{BASE}/api/rooms", headers=auth(alice),
                              json={"type": "group", "name": "Test Group",
                                    "participant_ids": [bob_id]})
        check("create group", r.status_code == 200, r.text[:200])
        group_id = r.json()["id"]

        # --- upload file (uses room.participants to notify members) ---
        files = {"files": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
        r = await client.post(f"{BASE}/api/chat/{group_id}/upload", headers=auth(alice),
                              files=files, data={"text": "with file"})
        check("upload file (notify participants)", r.status_code == 200,
              f"{r.status_code} {r.text[:300]}")
        upload_msg = r.json().get("message", {})
        check("upload returns message", upload_msg.get("id") is not None,
              str(upload_msg)[:200])

        # --- websocket: send message + mark read ---
        async with websockets.connect(f"{WS}/ws/chat/{group_id}?token={alice}") as ws:
            await ws.send(json.dumps({"type": "chat_message", "message": "ws hello"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            event = json.loads(raw)
            check("ws chat_message",
                  event.get("type") == "message" and event.get("message") == "ws hello",
                  raw[:200])
            ws_msg_id = event["id"]

            await ws.send(json.dumps({"type": "mark_read",
                                      "last_read_message_id": ws_msg_id}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            check("ws mark_read", json.loads(raw).get("type") == "read_receipt",
                  raw[:200])

        # --- messages refetched after cache invalidation ---
        r = await client.get(f"{BASE}/api/rooms/{group_id}/messages", headers=auth(alice))
        check("get messages after ws",
              r.status_code == 200 and len(r.json()) >= 2, r.text[:300])

        # --- messages fetched again from cache (timestamps round-trip) ---
        r2 = await client.get(f"{BASE}/api/rooms/{group_id}/messages", headers=auth(alice))
        check("get messages cached with data",
              r2.status_code == 200 and len(r2.json()) >= 2
              and all(m.get("timestamp") for m in r2.json()),
              r2.text[:300])

        # --- edit message ---
        r = await client.patch(f"{BASE}/api/rooms/{group_id}/messages/{upload_msg['id']}",
                               headers=auth(alice), json={"text": "edited!"})
        check("edit message",
              r.status_code == 200 and r.json().get("message") == "edited!",
              r.text[:300])

        # --- delete message ---
        r = await client.delete(f"{BASE}/api/rooms/{group_id}/messages/{upload_msg['id']}",
                                headers=auth(alice))
        check("delete message", r.status_code == 200, r.text[:200])

        # --- rename group ---
        r = await client.patch(f"{BASE}/api/rooms/{group_id}", headers=auth(alice),
                               json={"name": "Renamed Group"})
        check("rename group",
              r.status_code == 200 and r.json()["display"] == "Renamed Group",
              r.text[:300])

        # --- leave group (room.type / room.participants) ---
        r = await client.post(f"{BASE}/api/rooms/{group_id}/leave", headers=auth(bob))
        check("bob leaves group", r.status_code == 200, r.text[:300])

        # --- delete group ---
        r = await client.delete(f"{BASE}/api/rooms/{group_id}", headers=auth(alice))
        check("delete group", r.status_code == 200, r.text[:300])

        r = await client.get(f"{BASE}/api/rooms", headers=auth(alice))
        check("list rooms final", r.status_code == 200, r.text[:300])

        print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
