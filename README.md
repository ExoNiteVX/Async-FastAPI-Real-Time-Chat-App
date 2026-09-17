#Real-Time Async Chat Application

A high-performance real-time messaging web application featuring full-duplex WebSocket communication, async database handling via SQLAlchemy + SQLite (WAL mode), and a zero-dependency frontend UI.
Features

    Real-Time WebSockets: Dual WebSocket architecture supporting live messaging inside active chat rooms alongside a background user notification socket for global unread badges and updates.

    Direct & Group Messaging: Start 1-on-1 DMs or create, rename, and manage multi-user group chats.

    Message Editing & Deleting: Full lifecycle management for sent messages with live updates synced across all online connected participants.

    Read Receipts & Unread Badges: Live tick markers (✓ / ✓✓) indicating delivered and read statuses, accompanied by unread message counters per room.

    File Sharing: Upload up to 5 attachments per message (images, videos, audio, documents, code files) with native icons and formatted file sizes.

    Dark Mode UI: Single-page frontend written in vanilla JavaScript and CSS (no build tools required).

Tech Stack

    Backend: Python 3.10+, FastAPI, WebSockets, Pydantic

    Database: SQLite with aiosqlite driver and SQLAlchemy (Async Session) using WAL journaling mode

    Frontend: HTML5, CSS3, Modern Vanilla JavaScript (Fetch API, WebSockets)

Project Structure
Plaintext

├── connection_manager.py  # WebSocket connection routing (Room & User level)
├── crud.py                # Async database query helper functions
├── database.py            # Engine setup & SQLite PRAGMA configuration
├── index.html             # Single-page application UI & client logic
├── main.py                # FastAPI routes, file upload pipelines, WS logic
├── models.py              # SQLAlchemy ORM models (User, ChatRoom, Message, etc.)
└── seed.py                # Database reset & initial user seeding script

Quick Start
1. Installation

Clone the repository and install required python dependencies:
Bash

pip install fastapi uvicorn[standard] sqlalchemy aiosqlite pydantic

2. Seed Database

Run the database setup script to generate standard tables and test users:
Bash

python seed.py

Created Seed Users:

    Alice: secret

    Bob: bob

    Carol: carol

3. Run Application

Start the FastAPI ASGI development server:
Bash

uvicorn main:app --reload

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your web browser and sign in using one of the user tokens (e.g. secret). Open another browser window or incognito session to log in as a second user (e.g. bob) and start chatting in real time!
