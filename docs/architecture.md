# Architecture

## System Overview

C2 Lattice coordinates multiple Claude Code sessions on a single machine. Three components work together:

```
Claude Code Session A        Claude Code Session B        Claude Code Session C
     (Architect)                  (Worker)                     (Worker)
    mcp_server.py              mcp_server.py                mcp_server.py
    [stdio JSON-RPC]           [stdio JSON-RPC]             [stdio JSON-RPC]
          |                         |                              |
          +------------+------------+-------------+----------------+
                       |
                  broker.py
              127.0.0.1:7899
              ThreadingMixIn HTTP
              SQLite (WAL mode)
                       |
               dashboard.html
            (client-side rendering)
```

- **broker.py** -- Singleton HTTP server on localhost. Manages all state in SQLite. Handles registration, messaging, tasks, locks, memory, and serves the dashboard.
- **mcp_server.py** -- One instance per Claude Code session. Communicates with Claude via stdio JSON-RPC 2.0 (MCP protocol). Registers with the broker on startup, heartbeats every 3 seconds, polls for messages in the background.
- **dashboard.html** -- Standalone HTML file served by the broker at `/dashboard`. Fetches data from `/dashboard/data` every 3 seconds and renders everything client-side.

## Threading Model

The broker uses `socketserver.ThreadingMixIn` with `BaseHTTPRequestHandler`, spawning a new thread per incoming request. All database access is protected by a global `RLock` (reentrant lock), allowing nested acquisition from the same thread (needed because `log_activity` is called from within locked contexts).

Key threading details:
- `db_lock` (RLock) -- guards all SQLite reads and writes
- `rate_lock` (Lock) -- guards the in-memory rate limit buckets
- Socket timeout of 10 seconds prevents blocking writes from deadlocking
- The dead peer sweeper runs on a `threading.Timer` that re-schedules itself every 15 seconds

## SQLite Schema

The broker uses a single SQLite database with WAL journaling and 3-second busy timeout.

### peers

| Column | Type | Purpose |
|---|---|---|
| id | TEXT PK | Peer identifier (e.g., "architect", "worker-a3f2") |
| role | TEXT | "architect" or "worker" |
| pid | INTEGER | OS process ID for liveness checks |
| working_dir | TEXT | Working directory of the session |
| summary | TEXT | Human-readable status (max 200 chars) |
| last_heartbeat | REAL | Unix timestamp of last heartbeat |
| registered_at | REAL | Unix timestamp of registration |
| status | TEXT | "active" or "dead" |
| paused | INTEGER | 1 if paused by architect, 0 otherwise |
| poll_interval_ms | INTEGER | Heartbeat/poll interval in milliseconds |
| git_branch | TEXT | Current git branch |
| git_dirty_files | TEXT | Comma-separated list of modified files |
| git_last_commit | TEXT | Short hash of HEAD commit |
| token_budget | INTEGER | Optional token budget cap |
| tokens_used | INTEGER | Tokens consumed so far |
| tool_calls_count | INTEGER | Total tool calls made |
| errors_count | INTEGER | Total errors encountered |
| rejections_count | INTEGER | Total message rejections (content filter, rate limit) |
| last_stop_reason | TEXT | Why the peer was last stopped |

### messages

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | Auto-increment message ID |
| sender_id | TEXT FK | Sending peer |
| recipient_id | TEXT | Receiving peer (or "broadcast") |
| category | TEXT | status_update, question, finding, alert, blocker, error, review_request |
| content | TEXT | Message body (max 10KB) |
| run_id | TEXT | Optional run association |
| created_at | REAL | Unix timestamp |
| read_at | REAL | Unix timestamp when read (NULL if unread) |

### tasks

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | Auto-increment task ID |
| title | TEXT | Task title |
| description | TEXT | Detailed description |
| status | TEXT | pending, in_progress, completed |
| priority | TEXT | high, medium, low |
| created_by | TEXT FK | Peer that created the task |
| assigned_to | TEXT | Peer that claimed the task |
| blocked_by | TEXT | Comma-separated task IDs that must complete first |
| artifacts | TEXT | JSON string of completion artifacts |
| run_id | TEXT | Optional run association |
| created_at | REAL | Unix timestamp |
| updated_at | REAL | Unix timestamp |

### shared_memory

| Column | Type | Purpose |
|---|---|---|
| key | TEXT PK | Memory key |
| value | TEXT | Stored value (max 50KB) |
| peer_id | TEXT | Peer that last wrote this key |
| updated_at | REAL | Unix timestamp |
| type | TEXT | decision, fact, constraint, artifact |
| version | INTEGER | Auto-incrementing version number |
| confidence | TEXT | high, medium, low |
| supersedes | TEXT | Key of entry this replaces |

### shared_memory_history

Tracks all previous versions of shared memory entries for audit trail.

### file_locks

| Column | Type | Purpose |
|---|---|---|
| file_path | TEXT PK | Normalized file path |
| peer_id | TEXT FK | Peer holding the lock |
| locked_at | REAL | Unix timestamp |

### conversations

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| peer_id | TEXT FK | Peer whose conversation this is |
| turn_type | TEXT | user, assistant, tool_call, tool_result, summary |
| content | TEXT | Turn content (max 50KB) |
| timestamp | REAL | Unix timestamp |

### activity_log

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| timestamp | REAL | Unix timestamp |
| peer_id | TEXT | Peer involved |
| action | TEXT | Action type (registered, sent_message, died, etc.) |
| details | TEXT | JSON or plain text details |

### runs

| Column | Type | Purpose |
|---|---|---|
| id | TEXT PK | Run identifier |
| name | TEXT | Human-readable name |
| goal | TEXT | Run objective |
| success_criteria | TEXT | How to measure success |
| status | TEXT | active or completed |
| created_by | TEXT | Peer that created the run |
| created_at | REAL | Unix timestamp |

## Auth Flow

```
1. MCP Server starts
   |
2. POST /register  {id, role, pid, working_dir}    (no auth required)
   |
3. Broker validates, inserts peer, generates HMAC-SHA256 token
   Token payload: {"sub": peer_id, "role": role, "iat": timestamp}
   Token format:  base64url(payload).hmac_sha256(payload, secret)
   |
4. Broker returns {ok: true, token: "..."}
   |
5. MCP Server stores token, includes it as Bearer header on all requests
   |
6. Every request: broker extracts token, validates HMAC signature,
   checks role against endpoint permissions, enforces identity match
```

The broker secret is generated randomly at startup (`os.urandom(32).hex()`) or read from the `C2_LATTICE_SECRET` environment variable.

## Content Filter

Five regex patterns are applied to all message content after NFKC Unicode normalization:

| Pattern | Blocks | Reason |
|---|---|---|
| `<tool_use>`, `<tool_call>`, `<function_calls>` | XML tool invocation tags | Prevents prompt injection via tool-use blocks |
| `"function": {`, `"tool_calls": [`, `"name": "...", "arguments"` | Function-call JSON structures | Blocks JSON-shaped tool invocations |
| Deep file paths (3+ segments) | `/a/b/c/d` or `C:\a\b\c\d` | Prevents path traversal payloads |
| Base64 strings (100+ chars) | Long encoded blobs | Blocks encoded payloads |
| `data:` URIs with base64 | `data:image/png;base64,...` | Prevents embedded binary content |

## Dead Peer Sweeper

A background timer runs every 15 seconds:

1. Finds all active peers whose `last_heartbeat` is older than 15 seconds
2. Marks them as "dead" with `last_stop_reason = 'heartbeat_timeout'`
3. Reassigns their `in_progress` tasks back to `pending` with `assigned_to = NULL`
4. Releases all file locks held by dead peers
5. Logs the death event with count of reassigned tasks
6. Cleans up activity logs and conversations older than 7 days
7. Removes dead peer records older than 1 hour

Additionally, when `/peers` is queried, the broker performs PID liveness checks (`os.kill(pid, 0)`) to catch peers whose process died without missing heartbeats.

## Task DAG Auto-Unblock

When a task is completed via `/tasks/complete`:

1. The task status is set to `completed` and artifacts are stored
2. The broker finds all tasks whose `blocked_by` field contains the completed task's ID
3. For each blocked task, the completed task ID is removed from the `blocked_by` string
4. If a task's `blocked_by` becomes empty, it is now claimable

The `blocked_by` field is a comma-separated string of task IDs. The broker uses delimiter-aware parsing to avoid false positives (e.g., task 1 matching task 10).

## Message Delivery

Messages are delivered via two mechanisms:

1. **Background polling.** The MCP server's heartbeat loop polls `/messages/{peer_id}` every 3 seconds. New messages are stored in `_pending_messages` (thread-safe list).
2. **Direct fetch.** When `check_messages` is called, it drains the pending list and also makes a direct broker request to catch anything since the last poll.

Special routing:
- **Blocker messages** are auto-escalated to the architect (forwarded even if sent to a specific peer)
- **Error messages** are auto-broadcast to all active peers (except sender and original recipient)
- **Broadcast** messages are expanded into individual messages for each active peer

## Dashboard Architecture

The dashboard is a single HTML file (`dashboard.html`) served at `/dashboard`. It operates entirely client-side:

1. On load, fetches a system token from `/dashboard/token`
2. Every 3 seconds, fetches `/dashboard/data` with Bearer auth
3. Renders peers, tasks, attention items, stats, and sparkline using DOM manipulation
4. No frameworks or build tools -- vanilla HTML, CSS, and JavaScript

The `/dashboard/data` endpoint returns a comprehensive JSON payload including:
- Peer list with status, git state, telemetry counters
- Task list (top 15 by priority)
- Attention items (blockers, reviews, paused agents, unread messages, claimable tasks)
- Aggregate stats (active peers, messages, tasks by status)
- Activity sparkline (24 fifteen-minute buckets)
