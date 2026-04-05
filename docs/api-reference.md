# API Reference

The broker exposes an HTTP API on `127.0.0.1:7899`. All endpoints accept and return JSON unless noted otherwise.

**Authentication:** Most endpoints require a `Authorization: Bearer <token>` header. Tokens are issued at registration. Endpoints marked "No auth" can be called without a token.

**Error format:** All errors return `{"error": "description"}` with an appropriate HTTP status code.

---

## Registration

### POST /register

Register a new peer or re-register an existing one. Returns an HMAC-SHA256 token for subsequent requests.

- **Auth:** No auth required
- **Request body:**
  ```json
  {
    "id": "worker-01",
    "role": "architect|worker",
    "pid": 12345,
    "working_dir": "/path/to/project",
    "summary": "",
    "git_branch": "main",
    "git_dirty_files": "",
    "git_last_commit": "abc1234"
  }
  ```
- **Response (200):** `{"ok": true, "id": "worker-01", "token": "<hmac_token>"}`
- **Errors:**
  - `400` -- Invalid ID or role
  - `409` -- Architect role already held by another peer

```bash
curl -X POST http://127.0.0.1:7899/register \
  -H "Content-Type: application/json" \
  -d '{"id":"worker-01","role":"worker","pid":12345,"working_dir":"/tmp"}'
```

### POST /heartbeat

Update last-seen timestamp and optionally git state. Returns pause state and poll interval.

- **Auth:** Required (any role)
- **Request body:** `{"id": "worker-01", "git_branch": "main", ...}`
- **Response (200):** `{"ok": true, "paused": false, "poll_interval_ms": 3000}`

---

## Messaging

### POST /send

Send a message to a peer or broadcast to all.

- **Auth:** Required (any role; broadcast requires architect)
- **Request body:**
  ```json
  {
    "sender_id": "worker-01",
    "recipient_id": "architect|broadcast",
    "category": "status_update",
    "content": "Task completed successfully"
  }
  ```
- **Categories:** `status_update`, `question`, `finding`, `alert`, `blocker`, `error`, `review_request`
- **Response (200):** `{"ok": true}`
- **Errors:**
  - `400` -- Missing required fields
  - `403` -- Category "command" blocked; broadcast requires architect
  - `404` -- Sender or recipient not found
  - `413` -- Message exceeds 10KB
  - `422` -- Content filter rejection
  - `429` -- Rate limit exceeded

**Auto-escalation:** `blocker` messages are auto-forwarded to the architect. `error` messages are auto-broadcast to all peers.

### GET /messages/{peer_id}

Fetch unread messages for a peer. Marks them as read.

- **Auth:** Required (any role)
- **Response (200):**
  ```json
  {
    "messages": [
      {
        "id": 1,
        "sender_id": "architect",
        "category": "status_update",
        "content": "...",
        "created_at": 1700000000.0
      }
    ]
  }
  ```

### GET /messages-all?limit=30

Get recent messages across all peers (read-only, does not mark as read).

- **Auth:** Required (any role)
- **Query params:** `limit` (1-100, default 30)

### GET /messages-for/{peer_id}?limit=20

Get sent and received messages for a specific peer (read-only).

- **Auth:** Required (any role)
- **Query params:** `limit` (1-100, default 20)

---

## Tasks

### POST /tasks

Create a new task.

- **Auth:** Required (any role)
- **Request body:**
  ```json
  {
    "title": "Implement auth module",
    "description": "Add JWT validation to API endpoints",
    "priority": "high",
    "created_by": "architect",
    "blocked_by": [1, 2],
    "run_id": "sprint-1"
  }
  ```
- **Priority:** `high`, `medium` (default), `low`
- **Response (200):** `{"ok": true, "task_id": 3}`
- **Errors:**
  - `400` -- Missing title/created_by, or blocked_by references non-existent task

### POST /tasks/claim

Claim a pending task. Fails if task has unmet dependencies or peer has exceeded budget.

- **Auth:** Required (any role)
- **Request body:** `{"task_id": 3, "peer_id": "worker-01"}`
- **Response (200):** `{"ok": true}`
- **Errors:**
  - `400` -- Missing fields
  - `403` -- Budget exceeded
  - `404` -- Task not found
  - `409` -- Task already claimed or blocked

### POST /tasks/complete

Mark a task as completed. Triggers auto-unblock of dependent tasks.

- **Auth:** Required (any role)
- **Request body:**
  ```json
  {
    "task_id": 3,
    "peer_id": "worker-01",
    "artifacts": {
      "summary": "Implemented JWT validation with RS256",
      "files_touched": ["auth.py", "middleware.py"],
      "tests_run": "12 passed, 0 failed"
    }
  }
  ```
- **Response (200):** `{"ok": true, "newly_unblocked": [{"id": 4, "title": "Deploy auth"}]}`
- **Errors:**
  - `400` -- Missing fields or summary
  - `403` -- Task not assigned to you
  - `404` -- Task not found
  - `409` -- Task already completed

### GET /tasks?status=pending&assigned_to=worker-01

List tasks with optional filters.

- **Auth:** Required (any role)
- **Query params:** `status` (pending/in_progress/completed), `assigned_to`
- **Response (200):** `{"tasks": [...]}`

### GET /tasks/{id}

Get full task details including dependencies and what it blocks.

- **Auth:** Required (any role)
- **Response (200):** `{"task": {..., "claimable": true, "blocks": [...]}}`

---

## File Locks

### POST /lock

Lock a file to prevent concurrent edits.

- **Auth:** Required (any role)
- **Request body:** `{"peer_id": "worker-01", "file_path": "src/auth.py"}`
- **Response (200):** `{"ok": true}`
- **Errors:** `409` -- File already locked by another peer

### POST /unlock

Release a file lock.

- **Auth:** Required (any role)
- **Request body:** `{"peer_id": "worker-01", "file_path": "src/auth.py"}`
- **Response (200):** `{"ok": true}`
- **Errors:** `403` -- File locked by a different peer

### GET /locks

List all active file locks.

- **Auth:** Required (any role)
- **Response (200):** `{"locks": [{"file_path": "src/auth.py", "peer_id": "worker-01", "locked_at": 1700000000.0}]}`

---

## Memory

### POST /memory

Write a key-value pair to shared memory. Supports versioning and typed entries.

- **Auth:** Required (any role)
- **Request body:**
  ```json
  {
    "key": "db-schema",
    "value": "PostgreSQL with pgvector extension",
    "peer_id": "architect",
    "type": "decision",
    "confidence": "high",
    "supersedes": "old-db-schema"
  }
  ```
- **Types:** `decision`, `fact` (default), `constraint`, `artifact`
- **Confidence:** `high` (default), `medium`, `low`
- **Response (200):** `{"ok": true, "version": 2}`

### GET /memory?key=db-schema&type=decision

Read shared memory. Pass `key` for a single entry or omit for all. Filter by `type`.

- **Auth:** Required (any role)
- **Response (single key):** `{"key": "db-schema", "value": "...", "version": 2, ...}`
- **Response (all):** `{"memory": [...]}`

### GET /memory/history?key=db-schema

Get version history for a memory key.

- **Auth:** Required (any role)

---

## Dashboard

### GET /dashboard

Serve the dashboard HTML page. No authentication required.

### GET /dashboard/data

Full JSON data for dashboard rendering. No authentication required.

- **Response (200):** Object with `active_count`, `total_messages`, `unread_messages`, `tasks_completed`, `tasks_inprogress`, `tasks_pending`, `peers`, `tasks`, `attention`, `sparkline`, and more.

### GET /dashboard/token

Get a system token for dashboard API calls. No authentication required.

---

## Management (Architect/System Only)

### POST /pause

Pause a peer (prevents task claiming).

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01"}`

### POST /resume

Resume a paused peer.

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01"}`

### POST /pause-all

Pause all active workers.

- **Auth:** Required (architect or system)

### POST /resume-all

Resume all paused workers.

- **Auth:** Required (architect or system)

### POST /kill-peer

Force-remove a peer.

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01"}`

### POST /unregister

Gracefully unregister a peer.

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01"}`

### POST /config

Update peer configuration (poll interval).

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01", "poll_interval_ms": 5000}`

### POST /budget

Set token budget for a peer.

- **Auth:** Required (architect or system)
- **Request body:** `{"peer_id": "worker-01", "token_budget": 100000}`

### POST /spawn

Spawn a new worker session.

- **Auth:** Required (architect or system)
- **Request body:** `{"role": "worker", "peer_id": "worker-03", "working_dir": "/path"}`

### POST /shutdown

Shut down the broker. System role only.

- **Auth:** Required (system only)

---

## System

### GET /health

Health check. Returns broker status and port. No authentication required.

- **Response (200):** `{"status": "ok", "port": 7899}`

### POST /summary

Set a peer's summary text.

- **Auth:** Required (any role)
- **Request body:** `{"id": "worker-01", "summary": "Building auth module"}`

### POST /conversation

Log a conversation turn.

- **Auth:** Required (any role)
- **Request body:**
  ```json
  {
    "peer_id": "worker-01",
    "turn_type": "assistant",
    "content": "I've completed the auth implementation..."
  }
  ```
- **Turn types:** `user`, `assistant`, `tool_call`, `tool_result`, `summary`

### GET /conversation/{peer_id}?requester=architect&last=20

Get conversation log. Workers can only view their own; architect can view any.

- **Auth:** Required (any role)
- **Query params:** `requester` (required), `last` (1-200, default 50)

### GET /log?requester=architect&last=50

Get activity log. Architect only.

- **Auth:** Required (architect or system)

### GET /peer/{peer_id}

Get detailed info for a single peer.

- **Auth:** Required (any role)

### GET /peers?scope=all

List active peers with optional scope filtering.

- **Auth:** Required (any role)
- **Query params:** `scope` (all/same_dir/same_repo), `working_dir`, `git_root`

---

## Runs

### POST /runs

Create a new run.

- **Auth:** Required (any role)
- **Request body:** `{"id": "sprint-1", "name": "Sprint 1", "goal": "Ship auth feature", "created_by": "architect"}`

### GET /runs

List all runs.

- **Auth:** Required (any role)

### GET /runs/{id}

Get run details.

- **Auth:** Required (any role)

### GET /runs/{id}/summary

Get run summary with task progress, memory, and recent messages.

- **Auth:** Required (any role)
