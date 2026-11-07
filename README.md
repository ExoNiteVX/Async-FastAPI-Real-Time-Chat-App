# Real-Time Async Chat Application

A high-performance real-time messaging web application featuring full-duplex WebSocket communication, async database handling via SQLAlchemy + SQLite (WAL mode), Redis caching, secure authentication with bcrypt, and a zero-dependency frontend UI.

## Features

### Authentication & Security
- **Email/Password Registration**: Secure user registration with email validation and password strength requirements
- **bcrypt Password Hashing**: Industry-standard password hashing with configurable cost factor
- **Timing Attack Prevention**: Constant-time login responses (~0.7s delay) to prevent user enumeration
- **Thread Pool for Crypto**: Password hashing/verification runs in thread pool to avoid blocking the event loop
- **Secure Token Authentication**: URL-safe tokens for API authentication with token refresh capability
- **Password Strength Requirements**: Minimum 8 characters, uppercase, lowercase, digit, and special character

### Real-Time Messaging
- **Dual WebSocket Architecture**: Room-level WebSocket for active chat + User-level WebSocket for global notifications
- **Direct & Group Messaging**: Start 1-on-1 DMs or create, rename, and manage multi-user group chats
- **Message Editing & Deleting**: Full lifecycle management with live updates synced across all participants
- **Read Receipts & Unread Badges**: Live tick markers (✓ / ✓✓) indicating delivered/read status with unread counters
- **Typing Indicators**: Real-time "user is typing..." notifications
- **Message Reactions**: Emoji reactions on messages (👍 ❤️ 😂 😮 😢 😡)
- **Reply/Thread Support**: Quote and reply to specific messages

### Media & Files
- **File Sharing**: Upload up to 5 attachments per message (images, videos, audio, documents, code files)
- **Image Gallery**: Fullscreen lightbox with zoom/pan for images
- **Inline Previews**: PDF thumbnails, audio/video players in chat
- **Drag & Drop**: Drop files directly into composer
- **Paste Images**: Ctrl+V to paste screenshots

### Caching & Performance
- **Redis Caching**: Multi-layer caching with Redis backend and in-memory fallback
- **Smart Cache Invalidation**: Pattern-based invalidation on data mutations
- **Strategic TTLs**: Per-data-type TTLs (messages: 1min, rooms: 2min, auth: 10min)
- **Virtualized Message List**: Efficient rendering for 1000+ message histories

### Frontend Experience
- **Dark/Light/System Themes**: CSS custom properties with localStorage persistence
- **Responsive Mobile-First**: Bottom sheet composer, swipe gestures, pull-to-refresh
- **Command Palette**: `Cmd+K` for search rooms, users, messages
- **Keyboard Shortcuts**: `Esc` close modals, `↑` edit last message, `/` focus search
- **Accessibility**: ARIA labels, live regions, focus management, reduced motion support
- **Sound Notifications**: Optional "ding" on new messages (with permission)
- **Single-Page Vanilla JS**: No build tools, no frameworks, no dependencies

## Tech Stack

**Backend:**
- Python 3.10+, FastAPI, WebSockets, Pydantic v2
- SQLAlchemy 2.0 (Async Session) with SQLite + aiosqlite
- Redis (async) with in-memory LRU fallback
- bcrypt for password hashing
- email-validator for email validation

**Database:**
- SQLite with WAL journaling mode for concurrent reads/writes
- SQLAlchemy declarative models with relationships

**Frontend:**
- HTML5, CSS3 (CSS Custom Properties for theming)
- Modern Vanilla JavaScript (ES Modules, Fetch API, WebSockets)
- No build step, no bundler, no framework dependencies

## Project Structure

```
├── auth.py                 # Authentication utilities (bcrypt, validation, tokens)
├── cache.py                # Redis caching layer with in-memory fallback
├── connection_manager.py   # WebSocket connection routing (Room & User level)
├── crud.py                 # Async database query helper functions
├── database.py             # Engine setup, SQLite PRAGMA, cache init/shutdown
├── index.html              # Single-page application UI & client logic
├── main.py                 # FastAPI routes, auth endpoints, WS logic
├── models.py               # SQLAlchemy ORM models (User, ChatRoom, Message, etc.)
├── requirements.txt        # Python dependencies
├── seed.py                 # Database reset & initial user seeding script
├── test_integration.py     # End-to-end HTTP/WebSocket integration test
└── README.md               # This file
```

## Quick Start

### 1. Installation

Clone the repository and install required python dependencies:

```bash
pip install -r requirements.txt
```

### 2. Optional: Start Redis (for caching)

```bash
# Docker (recommended)
docker run -d --name redis-chat -p 6379:6379 redis:7-alpine

# Or install locally
# Ubuntu/Debian: sudo apt install redis-server && sudo systemctl start redis
# macOS: brew install redis && brew services start redis
```

**Note**: The app works without Redis using an in-memory LRU cache fallback.

### 3. Run Application

Start the FastAPI ASGI development server:

```bash
uvicorn main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your web browser.

### 4. Create an Account

1. Click "Sign up" on the login page
2. Enter username (3-30 chars, alphanumeric + underscore, must start with letter)
3. Enter valid email address
4. Create password meeting requirements:
   - At least 8 characters
   - One uppercase letter
   - One lowercase letter
   - One number
   - One special character (!@#$%^&*()_+-=[]{};':"\|,.<>/?)
5. Click "Create Account"

### 5. Start Chatting

- Open another browser window or incognito session
- Create a second account
- Start a Direct Message or Group Chat
- Enjoy real-time messaging!

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/register` | Register new user |
| POST | `/api/auth/login` | Login with email/password |
| POST | `/api/auth/refresh-token` | Generate new auth token |

### Rooms
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/rooms` | List user's rooms with unread counts |
| POST | `/api/rooms` | Create DM or group room |
| PATCH | `/api/rooms/{id}` | Rename group (owner only) |
| DELETE | `/api/rooms/{id}` | Delete group (owner only) |
| POST | `/api/rooms/{id}/leave` | Leave group |
| GET | `/api/rooms/{id}/messages` | Get message history |
| POST | `/api/rooms/{id}/read` | Mark messages as read |

### Messages
| Method | Endpoint | Description |
|--------|----------|-------------|
| PATCH | `/api/rooms/{id}/messages/{msg_id}` | Edit message |
| DELETE | `/api/rooms/{id}/messages/{msg_id}` | Delete message |
| POST | `/api/chat/{id}/upload` | Upload files |

### WebSockets
| Endpoint | Description |
|----------|-------------|
| `/ws/chat/{room_id}?token=...` | Room messaging WebSocket |
| `/ws/user?token=...` | User notifications WebSocket |

## WebSocket Message Types

### Client → Server (Room WS)
```json
{ "type": "chat_message", "message": "Hello!" }
{ "type": "mark_read", "last_read_message_id": 123 }
```

### Server → Client (Room WS)
```json
{ "type": "message", "id": 1, "room_id": 1, "sender": "alice", "sender_id": 1, "message": "Hello!", "files": [], "timestamp": "2024-01-15T10:30:00+05:00", "read_by": [] }
{ "type": "read_receipt", "room_id": 1, "reader_id": 2, "reader_username": "bob", "last_read_message_id": 1 }
{ "type": "message_edited", "room_id": 1, "message": {...} }
{ "type": "message_deleted", "room_id": 1, "message_id": 1 }
```

### Server → Client (User WS)
```json
{ "type": "new_message", "room_id": 1, "message": {...} }
{ "type": "new_room", "room": {...} }
{ "type": "room_updated", "room_id": 1, "name": "New Name", "display": "New Name" }
{ "type": "room_deleted", "room_id": 1 }
{ "type": "participant_left", "room_id": 1, "user_id": 2, "username": "bob" }
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `DATABASE_URL` | `sqlite+aiosqlite:///./chat.db` | Database connection URL |

## Security Considerations

1. **Password Hashing**: Uses bcrypt with default cost factor (12), runs in thread pool
2. **Timing Attack Prevention**: Login always takes ~0.7s ± 0.1s regardless of user existence
3. **Token Security**: URL-safe tokens (32 bytes entropy) for API authentication
4. **SQL Injection**: Parameterized queries via SQLAlchemy ORM
5. **XSS Prevention**: Frontend escapes all user content, uses textContent not innerHTML
6. **CORS**: Configure appropriately for production deployment
7. **HTTPS**: Required for production (secure cookies, WSS)

## Development

### Running Tests

`test_integration.py` is an end-to-end test that drives a running server over
HTTP and WebSocket. It exercises every endpoint that touches cached data
(rooms, messages, profiles, read states) and is what catches "dict vs ORM
object" regressions such as `AttributeError: 'dict' object has no attribute
'participants'`.

```bash
pip install httpx websockets   # test-only dependencies

python seed.py                                  # (re)seed the demo database
uvicorn main:app --port 8000                    # terminal 1
python test_integration.py http://127.0.0.1:8000 # terminal 2
```

Expected output ends with `ALL TESTS PASSED`. It works both with Redis running
and with the in-memory fallback, so run it in both modes after changing the
caching or serialization code.

### Database Migrations
For schema changes, use Alembic:
```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Adding New Cache Keys
1. Define key builder in `cache.py`
2. Add invalidation in `crud.py` mutation functions
3. Set appropriate TTL

## License

MIT License - feel free to use and modify for your projects.