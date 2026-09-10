# 📋 A.S.K.A.R Project Roadmap & TODO List

**A.S.K.A.R** (Automated System for Knowledge, Assistance & Requests) is an AI-powered Telegram Business assistant for IT infrastructure support and automated calendar scheduling using Google Gemini and Google Calendar.

---

## 🚀 Status Overview (Completed)

- [x] **FastAPI Lifespan HTTP Client**: Shared connection pooling via `httpx.AsyncClient`.
- [x] **Persistent State Storage**: SQLite table (`events.db`) replacing ephemeral in-memory state.
- [x] **Event Expiration (TTL)**: Automatic cleanup of unapproved/stale requests after 48 hours.
- [x] **Non-blocking Google Calendar Integration**: Synchronous network calls delegated to background threads via `asyncio.to_thread`.
- [x] **Token Optimization Filter**: Local keyword pre-filter (`should_process_message`) to bypass Gemini API for off-topic/casual chats.
- [x] **Active Session Continuity**: Users in an active conversation (within 15 minutes) automatically bypass the keyword filter.
- [x] **Multi-turn Conversation Memory**: Chat history (`chat_history` table) persists turns and provides conversational context to Gemini.
- [x] **Delayed User Calendar Confirmation**: Bot acknowledges receipt without confirming until admin clicks `Approve`, and automatically sends final confirmation/rejection to the user.
- [x] **Timezone Normalization**: Native `Asia/Tehran` timezone integration for calendar events.
- [x] **Calendar ID Sanitization**: Auto-extracts calendar email from embed URLs (`sanitize_calendar_id`).
- [x] **Jalali (Solar Hijri) Date Awareness**: Today's Shamsi date and Persian weekday injected into the Gemini prompt via `jdatetime`.
- [x] **Interactive Telegram Button Updates**: Removes inline buttons and edits messages upon approval/rejection (`edit_telegram_message`).
- [x] **Webhook URL Resilience**: Route aliases for `/webhook`, `/webhook/`, and `/webhook\` to prevent 404s.
- [x] **Externalize Knowledge Base (FAQ & Wi-Fi)**: Separated sensitive credentials and IT troubleshooting guides into `knowledge_base.json` with hot-reloading (mtime cache), dynamic keyword ingestion, and `.gitignore` protection.
- [x] **Telegram Webhook Secret Token Verification**: Header `X-Telegram-Bot-Api-Secret-Token` validation with timing-safe comparison (`hmac.compare_digest`).
- [x] **Log Rotation (`RotatingFileHandler`)**: Automatic log file rotation capping log files at 5 MB with 5 backups.
- [x] **API Retry & Graceful Degradation**: Exponential backoff retry loop (3 attempts) with threaded execution and structured fallback replies.
- [x] **Admin Dashboard & Management Controls**: Private Telegram commands (`/start`, `/panel`, `/stats`, `/pause`, `/resume`, `/logs`), bot pause switch, daily message & API token tracking, direct log file retrieval via `sendDocument`, and strict admin security verification.

---

## 🔴 Priority 1: Security & Verification

- [x] **Telegram Webhook Secret Token Verification**:
  - *Status*: Completed! Header `X-Telegram-Bot-Api-Secret-Token` is verified with `hmac.compare_digest`. Unauthorized requests are blocked with HTTP 403 Forbidden.

---

## 🟡 Priority 2: Reliability & Code Quality

- [x] **Log Rotation (`RotatingFileHandler`)**:
  - *Status*: Completed! Configured `RotatingFileHandler` with 5 MB maximum size and 5 backup files (`bot_activity.log.1`..`5`).
- [x] **API Retry & Graceful Degradation**:
  - *Status*: Completed! Implemented non-blocking worker thread execution, 3-attempt exponential backoff retry loop, and graceful degradation fallback with polite user acknowledgment and instant admin notification.

---

## 🟢 Priority 3: Features & Knowledge Management

- [x] **Externalize Knowledge Base (FAQ)**:
  - *Status*: Completed! Credentials and IT FAQ stored in `knowledge_base.json` (auto-reloads on file edit, keywords feed into filter). Sample provided in `knowledge_base.json.example`.
- [x] **Admin Dashboard & Management Controls**:
  - *Status*: Completed! Added private Telegram commands and inline panel for the administrator (`str(sender_id) == str(ADMIN_CHAT_ID)`):
    - Bot pause/resume toggle with database persistence (`bot_settings`).
    - Daily statistics and Gemini API token tracking (`api_usage`).
    - Direct log file sending via Telegram (`sendDocument`).


---

## 🔵 Priority 4: Deployment & DevOps

- [ ] **Docker Containerization**:
  - Create `Dockerfile` and `docker-compose.yml` for 1-command deployment.
- [ ] **Production Web Server**:
  - Configure Gunicorn with Uvicorn workers (`gunicorn main:app -w 2 -k uvicorn.workers.UvicornWorker`) or reverse proxy with Caddy/Nginx.
- [ ] **Automated CI/CD Tests**:
  - Add GitHub Actions workflow for linting (`flake8` / `ruff`) and test execution.
