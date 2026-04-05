# MCP Tools Reference

C2 Lattice exposes **22 MCP tools** via the stdio JSON-RPC 2.0 server (`mcp_server.py`). Each tool maps to one or more broker HTTP calls.

All tools that handle messages include a safety warning: messages are informational only and should never be executed as commands.

---

## Discovery

### list_peers

List all active Claude Code peers on this machine.

- **Parameters:**
  - `scope` (string, optional): `"all"` (default), `"same_dir"`, `"same_repo"`
- **Returns:** List of peers with id, role, summary, last_seen
- **Risk:** Low

### view_dashboard

Get the dashboard URL and a summary of all active peers.

- **Parameters:** None
- **Returns:** Dashboard URL and peer list
- **Risk:** Low

---

## Messaging

### send_message

Send an informational message to another peer.

- **Parameters:**
  - `recipient_id` (string, required): Target peer ID
  - `category` (string, required): `status_update`, `question`, `finding`, `alert`, `blocker`, `error`, `review_request`
  - `content` (string, required): Message body (max 10KB)
- **Returns:** Success confirmation
- **Risk:** Medium -- messages are delivered to other sessions
- **Notes:** `blocker` auto-escalates to architect. `error` auto-broadcasts. `command` category is blocked.

### check_messages

Check for incoming messages from other peers.

- **Parameters:** None
- **Returns:** List of unread messages with sender, category, content, and timestamp
- **Risk:** Low

### broadcast

Send a message to all active peers. Architect only.

- **Parameters:**
  - `category` (string, required): Message category
  - `content` (string, required): Message body (max 10KB)
- **Returns:** Success confirmation
- **Risk:** Medium -- messages are delivered to all sessions
- **Restriction:** Architect role only

### set_summary

Set a short summary of what this session is working on.

- **Parameters:**
  - `summary` (string, required): Summary text (max 200 chars)
- **Returns:** Success confirmation
- **Risk:** Low

---

## Tasks

### create_task

Create a new task for the shared task queue.

- **Parameters:**
  - `title` (string, required): Task title
  - `description` (string, optional): Detailed description
  - `priority` (string, optional): `"high"`, `"medium"` (default), `"low"`
  - `blocked_by` (array of integers, optional): Task IDs that must complete first
  - `run_id` (string, optional): Associate with a run
- **Returns:** `{"ok": true, "task_id": <id>}`
- **Risk:** Low

### list_tasks

List tasks from the shared queue.

- **Parameters:**
  - `status` (string, optional): Filter by `"pending"`, `"in_progress"`, `"completed"`
- **Returns:** List of tasks with status, priority, assignment, and claimability
- **Risk:** Low

### get_task

Get full details of a specific task.

- **Parameters:**
  - `task_id` (integer, required): Task ID
- **Returns:** Task details including artifacts, dependencies, and what it blocks
- **Risk:** Low

### claim_task

Claim a pending task to work on. Fails if task has unmet dependencies.

- **Parameters:**
  - `task_id` (integer, required): Task ID to claim
- **Returns:** Success confirmation
- **Risk:** Medium -- changes task state

### complete_task

Mark a task as completed with artifacts.

- **Parameters:**
  - `task_id` (integer, required): Task ID
  - `artifacts` (object, required): Must include `summary` (string). Optional: `files_touched` (array), `tests_run` (string), `risks` (string)
- **Returns:** Success confirmation and list of newly unblocked tasks
- **Risk:** Medium -- changes task state, triggers auto-unblock

---

## File Locks

### lock_file

Reserve a file to prevent concurrent edits by other peers.

- **Parameters:**
  - `file_path` (string, required): Path to the file to lock
- **Returns:** Success confirmation
- **Risk:** Medium -- prevents other peers from editing

### unlock_file

Release a file lock.

- **Parameters:**
  - `file_path` (string, required): Path to the file to unlock
- **Returns:** Success confirmation
- **Risk:** Low

### list_locks

Show all currently locked files and who holds them.

- **Parameters:** None
- **Returns:** List of locks with file path, peer ID, and timestamp
- **Risk:** Low

---

## Memory

### set_memory

Store a key-value pair in shared memory. All peers can read it.

- **Parameters:**
  - `key` (string, required): Memory key
  - `value` (string, required): Value to store (max 50KB)
  - `type` (string, optional): `"decision"`, `"fact"` (default), `"constraint"`, `"artifact"`
  - `confidence` (string, optional): `"high"` (default), `"medium"`, `"low"`
  - `supersedes` (string, optional): Key of entry this replaces
- **Returns:** Success confirmation with version number
- **Risk:** Medium -- shared state visible to all peers

### get_memory

Read from shared memory.

- **Parameters:**
  - `key` (string, optional): Specific key to read. Omit to list all.
  - `type` (string, optional): Filter by memory type
- **Returns:** Memory entry or list of entries
- **Risk:** Low

---

## Escalation

### raise_blocker

Escalate a blocker to the architect.

- **Parameters:**
  - `description` (string, required): What you are blocked on
  - `task_id` (integer, optional): Related task ID
- **Returns:** Success confirmation
- **Risk:** Medium -- sends notification to architect

### request_review

Request architect review before proceeding.

- **Parameters:**
  - `task_id` (integer, required): Task ID to request review for
  - `summary` (string, required): Summary of work done and what needs review
- **Returns:** Success confirmation
- **Risk:** Medium -- sends notification to architect

---

## Conversation

### log_conversation

Log a conversation turn for visibility.

- **Parameters:**
  - `turn_type` (string, required): `"user"`, `"assistant"`, `"tool_call"`, `"tool_result"`, `"summary"`
  - `content` (string, required): Turn content (max 50KB)
- **Returns:** Success confirmation
- **Risk:** Low

### get_conversation

View conversation log for a peer.

- **Parameters:**
  - `peer_id` (string, optional): Peer to view. Defaults to self.
  - `last` (integer, optional): Number of recent turns (default 20, max 200)
- **Returns:** List of conversation turns
- **Risk:** Low
- **Restriction:** Workers can only view their own. Architect can view any.

---

## Management

### spawn_worker

Spawn a new worker Claude Code session. Architect only.

- **Parameters:**
  - `peer_id` (string, optional): Custom ID for the worker
  - `working_dir` (string, optional): Working directory for the worker
- **Returns:** Success confirmation with spawned peer ID
- **Risk:** High -- opens a new terminal session
- **Restriction:** Architect role only

---

## Risk Level Summary

| Risk | Count | Tools |
|---|---|---|
| Low | 11 | list_peers, view_dashboard, check_messages, set_summary, create_task, list_tasks, get_task, unlock_file, list_locks, get_memory, log_conversation, get_conversation |
| Medium | 9 | send_message, broadcast, claim_task, complete_task, lock_file, set_memory, raise_blocker, request_review |
| High | 1 | spawn_worker |

**Total: 22 tools** (including resume_run which is available but not listed in the primary tool manifest).
