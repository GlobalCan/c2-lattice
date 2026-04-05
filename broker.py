#!/usr/bin/env python3
"""
C2 Lattice — Broker Daemon

Singleton HTTP server on 127.0.0.1:7899 backed by SQLite.
Tracks registered Claude Code peers, routes messages, enforces security.

Python stdlib only — no pip dependencies.
Run directly: python broker.py
"""

VERSION = "4.2.0"

import json
import os
import random
import re
import shutil
import signal
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import unicodedata
import base64
import hashlib
import hmac
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from threading import Lock, RLock, Timer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("C2_LATTICE_PORT", "7899"))
DB_PATH = os.environ.get(
    "C2_LATTICE_DB",
    os.path.join(os.path.expanduser("~"), ".c2-lattice.db"),
)
HEARTBEAT_TIMEOUT = 15  # seconds — 5 missed heartbeats at 3s interval
DEAD_CLEAN_INTERVAL = 15  # seconds between dead-peer sweeps
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10  # messages per window per peer
MAX_MESSAGE_SIZE = 10 * 1024  # 10KB
MAX_REQUEST_BODY = 100 * 1024  # 100KB max for any POST body
LOG_RETENTION_DAYS = 7  # auto-cleanup old activity logs
HEARTBEAT_LOG_INTERVAL = 6  # only log every Nth heartbeat per peer

# ---------------------------------------------------------------------------
# Token-based authentication (HMAC-SHA256)
# ---------------------------------------------------------------------------

BROKER_SECRET = os.environ.get("C2_LATTICE_SECRET", os.urandom(32).hex())

# Scope map: paths that don't require auth
NO_AUTH_PATHS = {"/register", "/dashboard", "/dashboard/data", "/dashboard/token", "/health"}

# POST endpoints requiring architect or system role
PRIVILEGED_POST = {"/pause", "/resume", "/ping", "/ping-all", "/config", "/budget",
                   "/kill-peer", "/unregister", "/pause-all", "/resume-all", "/spawn"}

# System-only endpoint
SYSTEM_ONLY = {"/shutdown"}

# GET endpoints requiring architect or system role
PRIVILEGED_GET = {"/log"}


def generate_token(peer_id: str, role: str) -> str:
    """Generate an HMAC-SHA256 signed token for a peer."""
    payload = json.dumps({"sub": peer_id, "role": role, "iat": int(time.time())})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    secret = BROKER_SECRET.encode() if isinstance(BROKER_SECRET, str) else BROKER_SECRET
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def validate_token(token: str) -> dict | None:
    """Validate a token and return payload dict or None if invalid."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        secret = BROKER_SECRET.encode() if isinstance(BROKER_SECRET, str) else BROKER_SECRET
        expected_sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        return payload  # {"sub": peer_id, "role": role, "iat": timestamp}
    except Exception:
        return None


# System token for command-center (generated once at startup, used by dashboard)
_system_token = ""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS peers (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            pid INTEGER,
            working_dir TEXT,
            summary TEXT DEFAULT '',
            last_heartbeat REAL,
            registered_at REAL,
            status TEXT DEFAULT 'active',
            paused INTEGER DEFAULT 0,
            poll_interval_ms INTEGER DEFAULT 3000,
            git_branch TEXT DEFAULT '',
            git_dirty_files TEXT DEFAULT '',
            git_last_commit TEXT DEFAULT '',
            token_budget INTEGER,
            tokens_used INTEGER DEFAULT 0,
            tool_calls_count INTEGER DEFAULT 0,
            errors_count INTEGER DEFAULT 0,
            rejections_count INTEGER DEFAULT 0,
            last_stop_reason TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            run_id TEXT DEFAULT '',
            created_at REAL,
            read_at REAL,
            FOREIGN KEY (sender_id) REFERENCES peers(id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            peer_id TEXT,
            action TEXT,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id TEXT NOT NULL,
            turn_type TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            FOREIGN KEY (peer_id) REFERENCES peers(id)
        );

        CREATE TABLE IF NOT EXISTS file_locks (
            file_path TEXT PRIMARY KEY,
            peer_id TEXT NOT NULL,
            locked_at REAL NOT NULL,
            FOREIGN KEY (peer_id) REFERENCES peers(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            created_by TEXT NOT NULL,
            assigned_to TEXT,
            blocked_by TEXT DEFAULT '',
            artifacts TEXT DEFAULT '',
            run_id TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (created_by) REFERENCES peers(id)
        );

        CREATE TABLE IF NOT EXISTS shared_memory (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            type TEXT DEFAULT 'fact',
            version INTEGER DEFAULT 1,
            confidence TEXT DEFAULT 'high',
            supersedes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS shared_memory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            type TEXT DEFAULT 'fact',
            version INTEGER DEFAULT 1,
            confidence TEXT DEFAULT 'high'
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            goal TEXT DEFAULT '',
            success_criteria TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_by TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()

    # Schema migrations for existing databases
    _migrations = [
        ("peers", "paused", "ALTER TABLE peers ADD COLUMN paused INTEGER DEFAULT 0"),
        ("peers", "poll_interval_ms", "ALTER TABLE peers ADD COLUMN poll_interval_ms INTEGER DEFAULT 10000"),
        ("peers", "git_branch", "ALTER TABLE peers ADD COLUMN git_branch TEXT DEFAULT ''"),
        ("peers", "git_dirty_files", "ALTER TABLE peers ADD COLUMN git_dirty_files TEXT DEFAULT ''"),
        ("peers", "git_last_commit", "ALTER TABLE peers ADD COLUMN git_last_commit TEXT DEFAULT ''"),
        # Phase 4: Budget caps
        ("peers", "token_budget", "ALTER TABLE peers ADD COLUMN token_budget INTEGER"),
        ("peers", "tokens_used", "ALTER TABLE peers ADD COLUMN tokens_used INTEGER DEFAULT 0"),
        # Phase 6: Versioned memory
        ("shared_memory", "type", "ALTER TABLE shared_memory ADD COLUMN type TEXT DEFAULT 'fact'"),
        ("shared_memory", "version", "ALTER TABLE shared_memory ADD COLUMN version INTEGER DEFAULT 1"),
        ("shared_memory", "confidence", "ALTER TABLE shared_memory ADD COLUMN confidence TEXT DEFAULT 'high'"),
        ("shared_memory", "supersedes", "ALTER TABLE shared_memory ADD COLUMN supersedes TEXT DEFAULT ''"),
        # Telemetry + denial tracking
        ("peers", "tool_calls_count", "ALTER TABLE peers ADD COLUMN tool_calls_count INTEGER DEFAULT 0"),
        ("peers", "errors_count", "ALTER TABLE peers ADD COLUMN errors_count INTEGER DEFAULT 0"),
        ("peers", "rejections_count", "ALTER TABLE peers ADD COLUMN rejections_count INTEGER DEFAULT 0"),
        ("peers", "last_stop_reason", "ALTER TABLE peers ADD COLUMN last_stop_reason TEXT DEFAULT ''"),
        # Phase 7: Run IDs
        ("tasks", "run_id", "ALTER TABLE tasks ADD COLUMN run_id TEXT DEFAULT ''"),
        ("messages", "run_id", "ALTER TABLE messages ADD COLUMN run_id TEXT DEFAULT ''"),
    ]
    for table, column, sql in _migrations:
        try:
            conn.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(sql)
    conn.commit()

    return conn


db = init_db(DB_PATH)
db_lock = RLock()  # Reentrant — log_activity is called from within db_lock contexts

# ---------------------------------------------------------------------------
# Rate limiting (in-memory, resets on restart — fine for localhost)
# ---------------------------------------------------------------------------

rate_buckets: dict[str, list[float]] = defaultdict(list)
rate_lock = Lock()


def check_rate_limit(peer_id: str) -> bool:
    """Return True if the peer is within rate limits."""
    now = time.time()
    with rate_lock:
        bucket = rate_buckets[peer_id]
        # Prune old entries
        rate_buckets[peer_id] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
        if len(rate_buckets[peer_id]) >= RATE_LIMIT_MAX:
            return False
        rate_buckets[peer_id].append(now)
        return True


# ---------------------------------------------------------------------------
# Content filtering
# ---------------------------------------------------------------------------

# Patterns that indicate prompt injection / tool abuse
TOOL_USE_PATTERN = re.compile(
    r"<tool_use>|</tool_use>|<tool_call>|</tool_call>|<function_calls>|</function_calls>",
    re.IGNORECASE,
)
FUNCTION_CALL_JSON_PATTERN = re.compile(
    r'"function"\s*:\s*\{|"tool_calls"\s*:\s*\[|"name"\s*:\s*"[^"]+"\s*,\s*"arguments"',
    re.IGNORECASE,
)
LONG_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:\\|/)(?:[^\s\\/:*?\"<>|]+[\\\/]){3,}[^\s\\/:*?\"<>|]*"
)
BASE64_LONG_PATTERN = re.compile(
    r"[A-Za-z0-9+/]{100,}={0,3}"
)
DATA_URI_PATTERN = re.compile(
    r"data:[a-zA-Z0-9/+.-]+;base64,"
)


def filter_content(content: str) -> tuple[bool, str]:
    """Return (ok, reason). If ok is False, the message should be rejected."""
    # Normalize Unicode to prevent homoglyph bypasses
    content = unicodedata.normalize("NFKC", content)
    if TOOL_USE_PATTERN.search(content):
        return False, "Message contains tool_use/tool_call blocks"
    if FUNCTION_CALL_JSON_PATTERN.search(content):
        return False, "Message contains function-call-shaped JSON"
    if LONG_PATH_PATTERN.search(content):
        return False, "Message contains long file paths (>3 segments)"
    if BASE64_LONG_PATTERN.search(content):
        return False, "Message contains long base64-encoded content"
    if DATA_URI_PATTERN.search(content):
        return False, "Message contains data URI with embedded content"
    return True, ""


# ---------------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------------


VALID_STOP_REASONS = {"budget_exceeded", "paused", "heartbeat_timeout", "killed", "unregistered", "completed", "crashed", "rate_limited", "content_blocked"}


def log_activity(peer_id: str | None, action: str, details: str | dict = "") -> None:
    """Log activity. details can be a string (legacy) or dict (structured JSON)."""
    if isinstance(details, dict):
        details_str = json.dumps(details, separators=(",", ":"))
    else:
        details_str = details
    with db_lock:
        db.execute(
            "INSERT INTO activity_log (timestamp, peer_id, action, details) VALUES (?, ?, ?, ?)",
            (time.time(), peer_id, action, details_str),
        )
        db.commit()


# ---------------------------------------------------------------------------
# Denial tracking (Build 2)
# ---------------------------------------------------------------------------

REJECTION_PAUSE_THRESHOLD = 20  # auto-pause after this many total rejections


def _increment_rejections(peer_id: str, stop_reason: str) -> None:
    """Increment rejection counter, auto-pause if threshold exceeded."""
    with db_lock:
        db.execute(
            "UPDATE peers SET rejections_count = rejections_count + 1, last_stop_reason = ? WHERE id = ?",
            (stop_reason, peer_id),
        )
        row = db.execute("SELECT rejections_count FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if row and row["rejections_count"] >= REJECTION_PAUSE_THRESHOLD:
            db.execute("UPDATE peers SET paused = 1 WHERE id = ?", (peer_id,))
            db.commit()
            log_activity(peer_id, "auto_paused", {"reason": "rejection_threshold", "rejections": row["rejections_count"], "stop_reason": stop_reason})
        else:
            db.commit()


# ---------------------------------------------------------------------------
# Dead-peer sweeper (runs on a timer)
# ---------------------------------------------------------------------------


def sweep_dead_peers() -> None:
    cutoff = time.time() - HEARTBEAT_TIMEOUT
    retention_cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    with db_lock:
        # Mark dead peers
        dead = db.execute(
            "SELECT id FROM peers WHERE status = 'active' AND id != 'command-center' AND last_heartbeat < ?",
            (cutoff,),
        ).fetchall()
        for row in dead:
            db.execute(
                "UPDATE peers SET status = 'dead', last_stop_reason = 'heartbeat_timeout' WHERE id = ?", (row["id"],)
            )
            # Build 3: Auto-reassign in_progress tasks from dead peers
            reassigned = db.execute(
                "UPDATE tasks SET status = 'pending', assigned_to = NULL WHERE assigned_to = ? AND status = 'in_progress'",
                (row["id"],),
            ).rowcount
            # Release file locks held by dead peer
            db.execute("DELETE FROM file_locks WHERE peer_id = ?", (row["id"],))
            log_activity(row["id"], "died", {"stop_reason": "heartbeat_timeout", "tasks_reassigned": reassigned})
        # Clean old activity logs (keep last 7 days)
        db.execute(
            "DELETE FROM activity_log WHERE timestamp < ?", (retention_cutoff,)
        )
        # Clean old conversations (keep last 7 days)
        db.execute(
            "DELETE FROM conversations WHERE timestamp < ?", (retention_cutoff,)
        )
        # Remove dead peers older than 1 hour
        db.execute(
            "DELETE FROM peers WHERE status = 'dead' AND last_heartbeat < ?",
            (time.time() - 3600,),
        )
        db.commit()
    # Re-schedule
    t = Timer(DEAD_CLEAN_INTERVAL, sweep_dead_peers)
    t.daemon = True
    t.start()


# Sweeper is started in main() to avoid race conditions at import time

# ---------------------------------------------------------------------------
# Dashboard path (served as static file from dashboard.html)
# ---------------------------------------------------------------------------

_DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")




# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _safe_int(val: str, default: int = 0, min_val: int = 0, max_val: int = 200) -> int:
    """Parse an integer safely with bounds clamping."""
    try:
        n = int(val)
        return max(min_val, min(n, max_val))
    except (ValueError, TypeError):
        return default


def _read_body(handler: BaseHTTPRequestHandler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except (ValueError, TypeError):
        return {}
    if length == 0:
        return {}
    if length > MAX_REQUEST_BODY:
        return None  # caller handles 413
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def _respond_json(handler: BaseHTTPRequestHandler, data: dict, status: int = 200) -> None:
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _respond_html(handler: BaseHTTPRequestHandler, html: str, status: int = 200) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _normalize_file_path(file_path: str) -> str:
    """Normalize file path for consistent lock matching."""
    # Normalize separators, resolve . and .., lowercase on Windows
    p = os.path.normpath(file_path).replace("\\", "/")
    if sys.platform == "win32":
        p = p.lower()
    return p


MAX_ID_LENGTH = 64
MAX_KEY_LENGTH = 256
MAX_PATH_LENGTH = 512
VALID_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_id(value: str, field: str = "id") -> str | None:
    """Validate an identifier. Returns error message or None if valid."""
    if not value:
        return f"{field} is required"
    if len(value) > MAX_ID_LENGTH:
        return f"{field} exceeds {MAX_ID_LENGTH} chars"
    if not VALID_ID_PATTERN.match(value):
        return f"{field} contains invalid characters (use alphanumeric, -, _, .)"
    return None


def _get_peer_role(peer_id: str) -> str | None:
    with db_lock:
        row = db.execute(
            "SELECT role FROM peers WHERE id = ? AND status = 'active'",
            (peer_id,),
        ).fetchone()
    return row["role"] if row else None


VALID_CATEGORIES = {"status_update", "question", "finding", "alert", "blocker", "error", "review_request"}


def _blocked_by_contains(blocked_by_str: str, task_id: int) -> bool:
    """Check if a comma-separated blocked_by string contains a specific task ID (delimiter-aware)."""
    if not blocked_by_str:
        return False
    return str(task_id) in [x.strip() for x in blocked_by_str.split(",")]


def _find_tasks_blocked_by(db_conn, task_id: int) -> list:
    """Find all tasks whose blocked_by contains task_id (delimiter-aware, no false positives)."""
    # Fetch candidates with LIKE for speed, then filter precisely
    candidates = db_conn.execute(
        "SELECT * FROM tasks WHERE blocked_by LIKE ?",
        (f"%{task_id}%",),
    ).fetchall()
    return [t for t in candidates if _blocked_by_contains(t["blocked_by"], task_id)]


class BrokerHandler(BaseHTTPRequestHandler):
    """Handle all broker HTTP requests."""

    def setup(self):
        """Set socket timeout to prevent blocking writes from deadlocking the DB."""
        super().setup()
        self.request.settimeout(10)

    def log_message(self, format, *args):
        # Log to stderr instead of stdout
        sys.stderr.write(f"[broker] {args[0]} {args[1]} {args[2]}\n")

    # ---- Authentication ----

    def _authenticate(self) -> dict | None:
        """Validate the Authorization: Bearer <token> header.
        Returns the token payload dict or None if invalid/missing."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:].strip()
        return validate_token(token)

    # ---- GET routes ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # Auth check for GET endpoints
        if path not in NO_AUTH_PATHS:
            if path in PRIVILEGED_GET:
                claims = self._authenticate()
                if claims is None:
                    _respond_json(self, {"error": "authentication required"}, 401)
                    return
                if claims.get("role") not in ("architect", "system"):
                    _respond_json(self, {"error": "insufficient privileges"}, 403)
                    return
            else:
                claims = self._authenticate()
                if claims is None:
                    _respond_json(self, {"error": "authentication required"}, 401)
                    return

        if path == "/peers":
            self._handle_get_peers(params)
        elif path.startswith("/messages/"):
            peer_id = path.split("/messages/", 1)[1]
            self._handle_get_messages(peer_id)
        elif path.startswith("/conversation/"):
            peer_id = path.split("/conversation/", 1)[1]
            self._handle_get_conversation(peer_id, params)
        elif path == "/locks":
            self._handle_get_locks()
        elif path == "/tasks":
            self._handle_get_tasks(params)
        elif path.startswith("/tasks/"):
            task_id_str = path.split("/tasks/", 1)[1]
            self._handle_get_task(task_id_str)
        elif path == "/memory":
            self._handle_get_memory(params)
        elif path == "/log":
            self._handle_get_log(params)
        elif path == "/dashboard":
            self._handle_dashboard()
        elif path == "/dashboard/data":
            self._handle_dashboard_data()
        elif path == "/dashboard/token":
            self._handle_dashboard_token()
        elif path == "/runs":
            self._handle_get_runs(params)
        elif path.startswith("/runs/"):
            remainder = path.split("/runs/", 1)[1]
            if remainder.endswith("/summary"):
                run_id = remainder[:-len("/summary")]
                self._handle_get_run_summary(run_id)
            else:
                self._handle_get_run(remainder)
        elif path == "/memory/history":
            self._handle_get_memory_history(params)
        elif path == "/health":
            _respond_json(self, {"status": "ok", "port": PORT})
        elif path.startswith("/peer/"):
            peer_id = path.split("/peer/", 1)[1]
            self._handle_get_peer(peer_id)
        elif path == "/messages-all":
            self._handle_get_messages_all(params)
        elif path.startswith("/messages-for/"):
            peer_id = path.split("/messages-for/", 1)[1]
            self._handle_get_messages_for_peer(peer_id, params)
        else:
            _respond_json(self, {"error": "not found"}, 404)

    @staticmethod
    def _is_pid_alive(pid: int | None) -> bool:
        """Check if a process is still running. Returns True if alive or if PID is unknown."""
        if pid is None:
            return True  # no PID stored, rely on heartbeat timeout
        try:
            os.kill(pid, 0)  # signal 0 = check existence, don't kill
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    def _handle_get_peers(self, params: dict) -> None:
        scope = params.get("scope", ["all"])[0]
        with db_lock:
            if scope == "all":
                peers = db.execute(
                    "SELECT * FROM peers WHERE status = 'active' AND id != 'command-center'"
                ).fetchall()
            elif scope == "same_dir":
                working_dir = params.get("working_dir", [""])[0]
                peers = db.execute(
                    "SELECT * FROM peers WHERE status = 'active' AND id != 'command-center' AND working_dir = ?",
                    (working_dir,),
                ).fetchall()
            elif scope == "same_repo":
                git_root = params.get("git_root", [""])[0]
                if git_root:
                    # Match peers whose working_dir starts with the git root
                    peers = db.execute(
                        "SELECT * FROM peers WHERE status = 'active' AND id != 'command-center' AND working_dir LIKE ?",
                        (git_root + "%",),
                    ).fetchall()
                else:
                    peers = db.execute(
                        "SELECT * FROM peers WHERE status = 'active' AND id != 'command-center'"
                    ).fetchall()
            else:
                peers = db.execute(
                    "SELECT * FROM peers WHERE status = 'active' AND id != 'command-center'"
                ).fetchall()

        # PID liveness check — mark dead peers inline
        alive = []
        for p in peers:
            if self._is_pid_alive(p["pid"]):
                alive.append(p)
            else:
                with db_lock:
                    db.execute("UPDATE peers SET status = 'dead' WHERE id = ?", (p["id"],))
                    db.commit()
                log_activity(p["id"], "died", "PID no longer alive")

        result = [
            {
                "id": p["id"],
                "role": p["role"],
                "summary": p["summary"],
                "working_dir": p["working_dir"],
                "last_heartbeat": p["last_heartbeat"],
                "registered_at": p["registered_at"],
                "paused": bool(p["paused"]),
                "poll_interval_ms": p["poll_interval_ms"],
                "git_branch": p["git_branch"],
                "git_dirty_files": p["git_dirty_files"],
                "git_last_commit": p["git_last_commit"],
            }
            for p in alive
        ]
        _respond_json(self, {"peers": result})

    def _handle_get_peer(self, peer_id: str) -> None:
        """Get single peer details including pause state, config, git."""
        with db_lock:
            peer = db.execute("SELECT * FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        _respond_json(self, {
            "id": peer["id"], "role": peer["role"], "status": peer["status"],
            "summary": peer["summary"], "paused": bool(peer["paused"]),
            "poll_interval_ms": peer["poll_interval_ms"],
            "git_branch": peer["git_branch"],
            "git_dirty_files": peer["git_dirty_files"],
            "git_last_commit": peer["git_last_commit"],
            "last_heartbeat": peer["last_heartbeat"],
            "pid": peer["pid"],
            "token_budget": peer["token_budget"],
            "tokens_used": peer["tokens_used"],
            "tool_calls_count": peer["tool_calls_count"],
            "errors_count": peer["errors_count"],
            "rejections_count": peer["rejections_count"],
            "last_stop_reason": peer["last_stop_reason"],
        })

    def _handle_get_messages(self, peer_id: str) -> None:
        now = time.time()
        with db_lock:
            messages = db.execute(
                "SELECT * FROM messages WHERE recipient_id = ? AND read_at IS NULL ORDER BY created_at ASC",
                (peer_id,),
            ).fetchall()
            # Mark as read
            for m in messages:
                db.execute(
                    "UPDATE messages SET read_at = ? WHERE id = ?", (now, m["id"])
                )
            db.commit()
        # log_activity and _respond_json outside lock
        for m in messages:
            log_activity(peer_id, "read_message", f"msg_id={m['id']} from={m['sender_id']}")
        result = [
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "category": m["category"],
                "content": m["content"],
                "created_at": m["created_at"],
            }
            for m in messages
        ]
        _respond_json(self, {"messages": result})

    def _handle_get_messages_all(self, params: dict) -> None:
        """Return recent messages across all peers (read-only, no marking as read)."""
        limit = _safe_int(params.get("limit", ["30"])[0], default=30, min_val=1, max_val=100)
        with db_lock:
            messages = db.execute(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = [
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "recipient_id": m["recipient_id"],
                "category": m["category"],
                "content": m["content"],
                "created_at": m["created_at"],
                "read_at": m["read_at"],
            }
            for m in messages
        ]
        _respond_json(self, {"messages": result})

    def _handle_get_messages_for_peer(self, peer_id: str, params: dict) -> None:
        """Return sent and received messages for a specific peer (read-only)."""
        limit = _safe_int(params.get("limit", ["20"])[0], default=20, min_val=1, max_val=100)
        with db_lock:
            messages = db.execute(
                "SELECT * FROM messages WHERE sender_id = ? OR recipient_id = ? ORDER BY created_at DESC LIMIT ?",
                (peer_id, peer_id, limit),
            ).fetchall()
        result = [
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "recipient_id": m["recipient_id"],
                "category": m["category"],
                "content": m["content"],
                "created_at": m["created_at"],
                "read_at": m["read_at"],
            }
            for m in messages
        ]
        _respond_json(self, {"messages": result})

    def _handle_get_log(self, params: dict) -> None:
        # Check architect role via query param
        requester = params.get("requester", [None])[0]
        if not requester:
            _respond_json(self, {"error": "requester param required"}, 400)
            return
        role = _get_peer_role(requester)
        if role != "architect":
            _respond_json(self, {"error": "architect-only endpoint"}, 403)
            return
        last = _safe_int(params.get("last", ["50"])[0], default=50, min_val=1, max_val=200)
        with db_lock:
            logs = db.execute(
                "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?",
                (last,),
            ).fetchall()
        result = [
            {
                "id": l["id"],
                "timestamp": l["timestamp"],
                "peer_id": l["peer_id"],
                "action": l["action"],
                "details": l["details"],
            }
            for l in logs
        ]
        _respond_json(self, {"logs": result})

    def _handle_get_conversation(self, peer_id: str, params: dict) -> None:
        """Get conversation log for a peer. Architect sees all; workers see only their own."""
        requester = params.get("requester", [None])[0]
        if not requester:
            _respond_json(self, {"error": "requester param required"}, 400)
            return
        # Permission check: own conversation OR architect
        if requester != peer_id:
            role = _get_peer_role(requester)
            if role != "architect":
                _respond_json(self, {"error": "can only view your own conversation (or be architect)"}, 403)
                return
        last = _safe_int(params.get("last", ["50"])[0], default=50, min_val=1, max_val=200)
        with db_lock:
            turns = db.execute(
                "SELECT * FROM conversations WHERE peer_id = ? ORDER BY timestamp DESC LIMIT ?",
                (peer_id, last),
            ).fetchall()
        result = [
            {
                "id": t["id"],
                "peer_id": t["peer_id"],
                "turn_type": t["turn_type"],
                "content": t["content"],
                "timestamp": t["timestamp"],
            }
            for t in reversed(turns)  # chronological order
        ]
        _respond_json(self, {"conversation": result})

    def _handle_dashboard(self) -> None:
        try:
            with open(_DASHBOARD_PATH, "r", encoding="utf-8") as f:
                html = f.read()
            _respond_html(self, html)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"dashboard.html not found")

    def _handle_dashboard_token(self) -> None:
        _respond_json(self, {"token": _system_token})

    def _handle_dashboard_data(self) -> None:
        """Full JSON data for live dashboard refresh — stats, peers, tasks, attention, viz."""
        now = time.time()
        with db_lock:
            active_count = db.execute("SELECT COUNT(*) as c FROM peers WHERE status = 'active' AND id != 'command-center'").fetchone()["c"]
            total_messages = db.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
            unread_messages = db.execute("SELECT COUNT(*) as c FROM messages WHERE read_at IS NULL").fetchone()["c"]
            tasks_completed = db.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'completed'").fetchone()["c"]
            tasks_inprogress = db.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'in_progress'").fetchone()["c"]
            tasks_pending = db.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'pending'").fetchone()["c"]
            active_runs = db.execute("SELECT COUNT(*) as c FROM runs WHERE status = 'active'").fetchone()["c"]
            peers = db.execute("SELECT * FROM peers WHERE id != 'command-center' ORDER BY status ASC, last_heartbeat DESC").fetchall()
            all_tasks = db.execute(
                "SELECT * FROM tasks ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC LIMIT 15"
            ).fetchall()
            escalations = db.execute(
                "SELECT * FROM messages WHERE category IN ('blocker','review_request') AND read_at IS NULL ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            unread_important = db.execute(
                "SELECT * FROM messages WHERE read_at IS NULL AND category NOT IN ('blocker','review_request') "
                "AND recipient_id IN ('command-center','broadcast') ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            # Sparkline: bucket activity into 24 intervals over last ~6 hours
            activity_rows = db.execute(
                "SELECT timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 200"
            ).fetchall()

        # Sparkline computation
        sparkline = [0] * 24
        for a in activity_rows:
            age = now - a["timestamp"]
            idx = int(age / 900)  # 15-min buckets
            if 0 <= idx < 24:
                sparkline[23 - idx] += 1

        total_tasks = tasks_completed + tasks_inprogress + tasks_pending

        # Build attention items
        attention = []

        # 1. Blockers (highest priority)
        for e in escalations:
            if e["category"] == "blocker":
                elapsed = now - (e["created_at"] or 0)
                ago = f"{int(elapsed)}s ago" if elapsed < 60 else (f"{int(elapsed/60)}m ago" if elapsed < 3600 else f"{int(elapsed/3600)}h ago")
                attention.append({
                    "type": "blocker", "peer_id": e["sender_id"],
                    "title": f"{e['sender_id']}: blocker",
                    "detail": (e["content"] or "")[:120] + " " + ago,
                })

        # 2. Review requests
        for e in escalations:
            if e["category"] == "review_request":
                elapsed = now - (e["created_at"] or 0)
                ago = f"{int(elapsed)}s ago" if elapsed < 60 else (f"{int(elapsed/60)}m ago" if elapsed < 3600 else f"{int(elapsed/3600)}h ago")
                attention.append({
                    "type": "review", "peer_id": e["sender_id"],
                    "title": f"{e['sender_id']}: review requested",
                    "detail": (e["content"] or "")[:120] + " " + ago,
                })

        # 3. Paused agents
        for p in peers:
            if p["paused"] and p["id"] != "command-center":
                attention.append({
                    "type": "paused", "peer_id": p["id"],
                    "title": f"{p['id']} is paused",
                    "detail": p["summary"] or "Waiting to be resumed",
                    "action": "resume",
                })

        # 4. Unread messages to command-center
        for m in unread_important:
            elapsed = now - (m["created_at"] or 0)
            ago = f"{int(elapsed)}s ago" if elapsed < 60 else (f"{int(elapsed/60)}m ago" if elapsed < 3600 else f"{int(elapsed/3600)}h ago")
            attention.append({
                "type": "unread", "peer_id": m["sender_id"],
                "title": f"{m['sender_id']}: {m['category']}",
                "detail": (m["content"] or "")[:120] + " " + ago,
            })

        # 5. Claimable tasks
        for t in all_tasks:
            if t["status"] == "pending" and not t["assigned_to"] and not t["blocked_by"]:
                attention.append({
                    "type": "task",
                    "title": f"Task #{t['id']}: {(t['title'] or '')[:50]}",
                    "detail": f"Priority: {t['priority']} \u2014 unassigned, ready to claim",
                })
                if len([a for a in attention if a["type"] == "task"]) >= 3:
                    break

        # Build peer list with extra fields for client-side rendering
        peer_list = []
        for p in peers:
            peer_list.append({
                "id": p["id"], "role": p["role"], "summary": p["summary"],
                "status": p["status"],
                "paused": bool(p["paused"]), "git_branch": p["git_branch"],
                "git_dirty_files": p["git_dirty_files"] or "",
                "seconds_ago": round(now - (p["last_heartbeat"] or 0), 1),
                "last_heartbeat": p["last_heartbeat"],
                "pid": p["pid"],
                "token_budget": p["token_budget"], "tokens_used": p["tokens_used"],
                "poll_interval_ms": p["poll_interval_ms"],
                "tool_calls_count": p["tool_calls_count"], "errors_count": p["errors_count"],
                "rejections_count": p["rejections_count"], "last_stop_reason": p["last_stop_reason"],
            })

        # Build tasks list
        task_list = []
        for t in all_tasks:
            task_list.append({
                "id": t["id"], "title": t["title"], "status": t["status"],
                "priority": t["priority"], "assigned_to": t["assigned_to"],
            })

        result = {
            "active_count": active_count,
            "total_messages": total_messages,
            "unread_messages": unread_messages,
            "total_tasks": total_tasks,
            "tasks_completed": tasks_completed,
            "tasks_inprogress": tasks_inprogress,
            "tasks_pending": tasks_pending,
            "active_runs": active_runs,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "peers": peer_list,
            "tasks": task_list,
            "attention": attention,
            "escalation_count": len(escalations),
            "sparkline": sparkline,
        }
        _respond_json(self, result)

    # ---- POST routes ----

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            body = _read_body(self)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _respond_json(self, {"error": f"invalid JSON: {e}"}, 400)
            return
        if body is None:
            _respond_json(self, {"error": f"request body exceeds {MAX_REQUEST_BODY} byte limit"}, 413)
            return

        # Auth check for POST endpoints
        if path not in NO_AUTH_PATHS:
            claims = self._authenticate()
            if claims is None:
                _respond_json(self, {"error": "authentication required"}, 401)
                return

            # System-only check
            if path in SYSTEM_ONLY:
                if claims.get("role") != "system":
                    _respond_json(self, {"error": "system role required"}, 403)
                    return

            # Privileged endpoint check
            if path in PRIVILEGED_POST:
                if claims.get("role") not in ("architect", "system"):
                    _respond_json(self, {"error": "insufficient privileges"}, 403)
                    return

            # Identity enforcement: peer_id/sender_id in body must match token sub
            # unless caller is architect or system role
            if claims.get("role") not in ("architect", "system"):
                body_peer_id = body.get("peer_id") or body.get("sender_id") or body.get("id")
                if body_peer_id and body_peer_id != claims.get("sub"):
                    _respond_json(self, {"error": "peer_id mismatch with token identity"}, 403)
                    return

        if path == "/register":
            self._handle_register(body)
        elif path == "/heartbeat":
            self._handle_heartbeat(body)
        elif path == "/send":
            self._handle_send(body)
        elif path == "/summary":
            self._handle_summary(body)
        elif path == "/conversation":
            self._handle_post_conversation(body)
        elif path == "/lock":
            self._handle_lock(body)
        elif path == "/unlock":
            self._handle_unlock(body)
        elif path == "/tasks":
            self._handle_post_task(body)
        elif path == "/tasks/claim":
            self._handle_claim_task(body)
        elif path == "/tasks/complete":
            self._handle_complete_task(body)
        elif path == "/memory":
            self._handle_post_memory(body)
        elif path == "/pause":
            self._handle_pause(body)
        elif path == "/resume":
            self._handle_resume(body)
        elif path == "/ping-all":
            self._handle_ping_all(body)
        elif path == "/ping":
            self._handle_ping(body)
        elif path == "/config":
            self._handle_config(body)
        elif path == "/budget":
            self._handle_budget(body)
        elif path == "/runs":
            self._handle_post_run(body)
        elif path == "/kill-peer":
            self._handle_kill_peer(body)
        elif path == "/unregister":
            self._handle_unregister(body)
        elif path == "/pause-all":
            self._handle_pause_all(body)
        elif path == "/resume-all":
            self._handle_resume_all(body)
        elif path == "/shutdown":
            self._handle_shutdown(body)
        elif path == "/spawn":
            self._handle_spawn(body)
        else:
            _respond_json(self, {"error": "not found"}, 404)

    def _handle_register(self, body: dict) -> None:
        peer_id = body.get("id", "").strip()
        role = body.get("role", "worker").strip()
        working_dir = body.get("working_dir", "")
        summary = body.get("summary", "")

        err = _validate_id(peer_id, "id")
        if err:
            _respond_json(self, {"error": err}, 400)
            return

        if role not in ("architect", "worker"):
            _respond_json(self, {"error": "role must be 'architect' or 'worker'"}, 400)
            return

        # Architect privilege: only one architect at a time
        if role == "architect":
            with db_lock:
                existing = db.execute(
                    "SELECT id FROM peers WHERE role = 'architect' AND status = 'active'"
                ).fetchone()
            if existing and existing["id"] != peer_id:
                _respond_json(
                    self,
                    {"error": f"Architect role already held by '{existing['id']}'"},
                    409,
                )
                return

        now = time.time()
        with db_lock:
            # Upsert — re-registration is fine
            pid = body.get("pid")
            db.execute(
                """INSERT INTO peers (id, role, pid, working_dir, summary, last_heartbeat, registered_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                   ON CONFLICT(id) DO UPDATE SET
                     role = excluded.role,
                     pid = excluded.pid,
                     working_dir = excluded.working_dir,
                     summary = excluded.summary,
                     last_heartbeat = excluded.last_heartbeat,
                     status = 'active'
                """,
                (peer_id, role, pid, working_dir, summary, now, now),
            )
            db.commit()
        log_activity(peer_id, "registered", f"role={role} dir={working_dir}")
        token = generate_token(peer_id, role)
        _respond_json(self, {"ok": True, "id": peer_id, "token": token})

    # Track heartbeat counts for sparse logging (protected by db_lock)
    _heartbeat_counts: dict[str, int] = {}
    _heartbeat_lock = Lock()

    def _handle_send(self, body: dict) -> None:
        sender_id = body.get("sender_id", "")
        recipient_id = body.get("recipient_id", "")
        category = body.get("category", "")
        content = body.get("content", "")

        # Validate required fields
        if not all([sender_id, recipient_id, category, content]):
            _respond_json(self, {"error": "sender_id, recipient_id, category, content are all required"}, 400)
            return

        # Block "command" category
        if category == "command":
            _respond_json(self, {"error": "category 'command' is not allowed"}, 403)
            return

        # Validate category
        if category not in VALID_CATEGORIES:
            _respond_json(
                self,
                {"error": f"invalid category, must be one of: {', '.join(sorted(VALID_CATEGORIES))}"},
                400,
            )
            return

        # Check sender exists and is active (command-center is always allowed)
        if sender_id == "command-center":
            sender_role = "architect"
        else:
            sender_role = _get_peer_role(sender_id)
            if sender_role is None:
                _respond_json(self, {"error": f"sender '{sender_id}' not found or inactive"}, 404)
                return

        # Broadcast only allowed from architect
        if recipient_id == "broadcast":
            if sender_role != "architect":
                _respond_json(self, {"error": "only architect can broadcast"}, 403)
                return
        else:
            # Check recipient exists and is active
            recipient_role = _get_peer_role(recipient_id)
            if recipient_role is None:
                _respond_json(self, {"error": f"recipient '{recipient_id}' not found or inactive"}, 404)
                return

        # Size limit
        if len(content.encode("utf-8")) > MAX_MESSAGE_SIZE:
            _respond_json(self, {"error": f"message exceeds {MAX_MESSAGE_SIZE} byte limit"}, 413)
            return

        # Rate limit
        if not check_rate_limit(sender_id):
            _increment_rejections(sender_id, "rate_limited")
            _respond_json(
                self,
                {"error": f"rate limit exceeded ({RATE_LIMIT_MAX} messages per {RATE_LIMIT_WINDOW}s)"},
                429,
            )
            return

        # Content filtering
        ok, reason = filter_content(content)
        if not ok:
            _increment_rejections(sender_id, "content_blocked")
            log_activity(sender_id, "message_rejected", {"reason": reason, "recipient": recipient_id, "stop_reason": "content_blocked"})
            _respond_json(self, {"error": f"message rejected: {reason}"}, 422)
            return

        # Send message(s)
        now = time.time()
        if recipient_id == "broadcast":
            with db_lock:
                active_peers = db.execute(
                    "SELECT id FROM peers WHERE status = 'active' AND id != ?",
                    (sender_id,),
                ).fetchall()
                for peer in active_peers:
                    db.execute(
                        "INSERT INTO messages (sender_id, recipient_id, category, content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (sender_id, peer["id"], category, content, now),
                    )
                db.commit()
            log_activity(sender_id, "broadcast", f"cat={category} to={len(active_peers)} peers")
        else:
            with db_lock:
                db.execute(
                    "INSERT INTO messages (sender_id, recipient_id, category, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (sender_id, recipient_id, category, content, now),
                )
                db.commit()
            log_activity(sender_id, "sent_message", f"to={recipient_id} cat={category}")

        # --- Phase 2: Auto-escalation for blocker/error categories ---
        if category == "blocker":
            # Auto-forward blockers to the architect (even if sent to a specific peer)
            with db_lock:
                architect = db.execute(
                    "SELECT id FROM peers WHERE role = 'architect' AND status = 'active'"
                ).fetchone()
            if architect and architect["id"] != recipient_id and architect["id"] != sender_id:
                with db_lock:
                    db.execute(
                        "INSERT INTO messages (sender_id, recipient_id, category, content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (sender_id, architect["id"], "blocker", f"[AUTO-ESCALATED] {content}", now),
                    )
                    db.commit()
                log_activity(sender_id, "blocker_escalated", f"auto-forwarded to architect {architect['id']}")

        elif category == "error":
            # Auto-broadcast errors to ALL peers (except sender and original recipient)
            with db_lock:
                other_peers = db.execute(
                    "SELECT id FROM peers WHERE status = 'active' AND id != ? AND id != ?",
                    (sender_id, recipient_id),
                ).fetchall()
                for peer in other_peers:
                    db.execute(
                        "INSERT INTO messages (sender_id, recipient_id, category, content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (sender_id, peer["id"], "error", f"[AUTO-BROADCAST] {content}", now),
                    )
                db.commit()
            if other_peers:
                log_activity(sender_id, "error_broadcast", f"auto-broadcast to {len(other_peers)} peers")

        _respond_json(self, {"ok": True})

    def _handle_summary(self, body: dict) -> None:
        peer_id = body.get("id", "")
        summary = body.get("summary", "")
        if not peer_id:
            _respond_json(self, {"error": "id is required"}, 400)
            return
        # Truncate to 200 chars
        summary = summary[:200]
        with db_lock:
            db.execute(
                "UPDATE peers SET summary = ? WHERE id = ?", (summary, peer_id)
            )
            db.commit()
        log_activity(peer_id, "set_summary", summary[:80])
        _respond_json(self, {"ok": True})  # already outside lock

    def _handle_post_conversation(self, body: dict) -> None:
        """Log a conversation turn from a peer."""
        peer_id = body.get("peer_id", "")
        turn_type = body.get("turn_type", "")
        content = body.get("content", "")
        if not all([peer_id, turn_type, content]):
            _respond_json(self, {"error": "peer_id, turn_type, content required"}, 400)
            return
        if turn_type not in ("user", "assistant", "tool_call", "tool_result", "summary"):
            _respond_json(self, {"error": "turn_type must be user/assistant/tool_call/tool_result/summary"}, 400)
            return
        # Rate limit conversation logging (same as messages)
        if not check_rate_limit(peer_id):
            _respond_json(self, {"error": "rate limit exceeded for conversation logging"}, 429)
            return
        # Truncate content to 50KB max for conversation logs
        content = content[:50000]
        now = time.time()
        with db_lock:
            db.execute(
                "INSERT INTO conversations (peer_id, turn_type, content, timestamp) VALUES (?, ?, ?, ?)",
                (peer_id, turn_type, content, now),
            )
            db.commit()
        _respond_json(self, {"ok": True})  # already outside lock

    # ---- File Locks ----

    def _handle_get_locks(self) -> None:
        with db_lock:
            locks = db.execute("SELECT * FROM file_locks ORDER BY locked_at DESC").fetchall()
        result = [{"file_path": l["file_path"], "peer_id": l["peer_id"], "locked_at": l["locked_at"]} for l in locks]
        _respond_json(self, {"locks": result})

    def _handle_lock(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        file_path = body.get("file_path", "")
        if not all([peer_id, file_path]):
            _respond_json(self, {"error": "peer_id and file_path required"}, 400)
            return
        file_path = _normalize_file_path(file_path)
        now = time.time()
        with db_lock:
            existing = db.execute("SELECT peer_id FROM file_locks WHERE file_path = ?", (file_path,)).fetchone()
            if existing and existing["peer_id"] != peer_id:
                lock_holder = existing["peer_id"]
            else:
                lock_holder = None
                db.execute(
                    "INSERT INTO file_locks (file_path, peer_id, locked_at) VALUES (?, ?, ?) ON CONFLICT(file_path) DO UPDATE SET peer_id=excluded.peer_id, locked_at=excluded.locked_at",
                    (file_path, peer_id, now),
                )
                db.commit()
        if lock_holder:
            _respond_json(self, {"error": f"file locked by {lock_holder}"}, 409)
            return
        log_activity(peer_id, "file_locked", file_path)
        _respond_json(self, {"ok": True})

    def _handle_unlock(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        file_path = body.get("file_path", "")
        if not all([peer_id, file_path]):
            _respond_json(self, {"error": "peer_id and file_path required"}, 400)
            return
        file_path = _normalize_file_path(file_path)
        with db_lock:
            existing = db.execute("SELECT peer_id FROM file_locks WHERE file_path = ?", (file_path,)).fetchone()
            if existing and existing["peer_id"] != peer_id:
                lock_holder = existing["peer_id"]
            else:
                lock_holder = None
                db.execute("DELETE FROM file_locks WHERE file_path = ? AND peer_id = ?", (file_path, peer_id))
                db.commit()
        if lock_holder:
            _respond_json(self, {"error": f"file locked by {lock_holder}, not you"}, 403)
            return
        log_activity(peer_id, "file_unlocked", file_path)
        _respond_json(self, {"ok": True})

    # ---- Task Queue ----

    @staticmethod
    def _is_task_unblocked(task_row, db_conn) -> bool:
        """Check if all blocking tasks are completed."""
        blocked_by = task_row["blocked_by"]
        if not blocked_by:
            return True
        try:
            dep_ids = [int(x.strip()) for x in blocked_by.split(",") if x.strip()]
        except ValueError:
            return True  # malformed blocked_by, treat as unblocked
        if not dep_ids:
            return True
        placeholders = ",".join("?" * len(dep_ids))
        completed = db_conn.execute(
            f"SELECT COUNT(*) as c FROM tasks WHERE id IN ({placeholders}) AND status = 'completed'",
            dep_ids,
        ).fetchone()["c"]
        return completed == len(dep_ids)

    @staticmethod
    def _format_task(t) -> dict:
        """Format a task row for JSON response."""
        return {
            "id": t["id"], "title": t["title"], "description": t["description"],
            "status": t["status"], "priority": t["priority"],
            "created_by": t["created_by"], "assigned_to": t["assigned_to"],
            "blocked_by": t["blocked_by"], "artifacts": t["artifacts"],
            "run_id": t["run_id"],
            "created_at": t["created_at"], "updated_at": t["updated_at"],
        }

    def _handle_get_task(self, task_id_str: str) -> None:
        """Get a single task by ID with full details."""
        try:
            task_id = int(task_id_str)
        except ValueError:
            _respond_json(self, {"error": "invalid task ID"}, 400)
            return
        with db_lock:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            _respond_json(self, {"error": "task not found"}, 404)
            return
        fmt = self._format_task(task)
        with db_lock:
            fmt["claimable"] = self._is_task_unblocked(task, db)
        # Parse artifacts JSON if present
        if fmt["artifacts"]:
            try:
                fmt["artifacts"] = json.loads(fmt["artifacts"])
            except json.JSONDecodeError:
                pass
        # Find tasks that depend on this one (delimiter-aware)
        with db_lock:
            dependents = _find_tasks_blocked_by(db, task_id)
        fmt["blocks"] = [{"id": d["id"], "title": d["title"], "status": d["status"]} for d in dependents]
        _respond_json(self, {"task": fmt})

    def _handle_get_tasks(self, params: dict) -> None:
        status_filter = params.get("status", [None])[0]
        assigned_to = params.get("assigned_to", [None])[0]
        with db_lock:
            if status_filter and assigned_to:
                tasks = db.execute("SELECT * FROM tasks WHERE status = ? AND assigned_to = ? ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC", (status_filter, assigned_to)).fetchall()
            elif status_filter:
                tasks = db.execute("SELECT * FROM tasks WHERE status = ? ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC", (status_filter,)).fetchall()
            elif assigned_to:
                tasks = db.execute("SELECT * FROM tasks WHERE assigned_to = ? ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC", (assigned_to,)).fetchall()
            else:
                tasks = db.execute("SELECT * FROM tasks ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC").fetchall()
        result = []
        for t in tasks:
            fmt = self._format_task(t)
            # For pending tasks, add whether they're actually claimable (unblocked)
            if t["status"] == "pending":
                with db_lock:
                    fmt["claimable"] = self._is_task_unblocked(t, db)
            result.append(fmt)
        _respond_json(self, {"tasks": result})

    def _handle_post_task(self, body: dict) -> None:
        title = body.get("title", "")
        description = body.get("description", "")
        priority = body.get("priority", "medium")
        created_by = body.get("created_by", "")
        blocked_by_list = body.get("blocked_by", [])
        if not all([title, created_by]):
            _respond_json(self, {"error": "title and created_by required"}, 400)
            return
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        # Validate blocked_by IDs exist
        blocked_by_str = ""
        if blocked_by_list:
            if isinstance(blocked_by_list, list):
                blocked_by_str = ",".join(str(x) for x in blocked_by_list)
            else:
                blocked_by_str = str(blocked_by_list)
            # Verify all referenced tasks exist
            dep_ids = [int(x.strip()) for x in blocked_by_str.split(",") if x.strip()]
            missing_dep = None
            with db_lock:
                for dep_id in dep_ids:
                    exists = db.execute("SELECT id FROM tasks WHERE id = ?", (dep_id,)).fetchone()
                    if not exists:
                        missing_dep = dep_id
                        break
            if missing_dep is not None:
                _respond_json(self, {"error": f"blocked_by references non-existent task #{missing_dep}"}, 400)
                return
        run_id = body.get("run_id", "")
        now = time.time()
        with db_lock:
            cursor = db.execute(
                "INSERT INTO tasks (title, description, status, priority, created_by, blocked_by, run_id, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (title, description, priority, created_by, blocked_by_str, run_id, now, now),
            )
            task_id = cursor.lastrowid
            db.commit()
        deps_note = f" blocked_by=[{blocked_by_str}]" if blocked_by_str else ""
        run_note = f" run={run_id}" if run_id else ""
        log_activity(created_by, "task_created", f"#{task_id}: {title}{deps_note}{run_note}")
        _respond_json(self, {"ok": True, "task_id": task_id})

    def _handle_claim_task(self, body: dict) -> None:
        task_id = body.get("task_id")
        peer_id = body.get("peer_id", "")
        if not all([task_id, peer_id]):
            _respond_json(self, {"error": "task_id and peer_id required"}, 400)
            return
        now = time.time()
        error_response = None
        with db_lock:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                error_response = ({"error": "task not found"}, 404)
            elif task["status"] != "pending":
                error_response = ({"error": f"task already {task['status']} by {task['assigned_to']}"}, 409)
            elif not self._is_task_unblocked(task, db):
                error_response = ({"error": f"task is blocked by tasks [{task['blocked_by']}] which are not all completed"}, 409)
            else:
                # Phase 4: Budget check
                peer = db.execute("SELECT token_budget, tokens_used FROM peers WHERE id = ?", (peer_id,)).fetchone()
                if peer and peer["token_budget"] is not None and peer["tokens_used"] >= peer["token_budget"]:
                    error_response = ({"error": f"budget exceeded ({peer['tokens_used']}/{peer['token_budget']} tokens). Ask architect for more."}, 403)
                else:
                    db.execute("UPDATE tasks SET status='in_progress', assigned_to=?, updated_at=? WHERE id=?", (peer_id, now, task_id))
                    db.commit()
        if error_response:
            _respond_json(self, error_response[0], error_response[1])
            if error_response[1] == 403:
                log_activity(peer_id, "budget_exceeded", f"tried to claim task #{task_id}")
            return
        log_activity(peer_id, "task_claimed", f"#{task_id}: {task['title']}")
        _respond_json(self, {"ok": True})

    def _handle_complete_task(self, body: dict) -> None:
        task_id = body.get("task_id")
        peer_id = body.get("peer_id", "")
        artifacts = body.get("artifacts", {})
        if not all([task_id, peer_id]):
            _respond_json(self, {"error": "task_id and peer_id required"}, 400)
            return
        # Artifacts required: at minimum a summary
        if not artifacts or not artifacts.get("summary"):
            _respond_json(self, {"error": "artifacts with at least 'summary' required to complete a task"}, 400)
            return
        now = time.time()
        artifacts_json = json.dumps(artifacts)
        newly_unblocked = []
        error_response = None
        with db_lock:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                error_response = ({"error": "task not found"}, 404)
            elif task["status"] == "completed":
                error_response = ({"error": "task already completed"}, 409)
            elif task["assigned_to"] != peer_id:
                error_response = ({"error": "task not assigned to you"}, 403)
            else:
                db.execute("UPDATE tasks SET status='completed', artifacts=?, updated_at=? WHERE id=?",
                           (artifacts_json, now, task_id))
                # Check if any blocked tasks are now unblocked (delimiter-aware)
                blocked_tasks = _find_tasks_blocked_by(db, task_id)
                blocked_tasks = [bt for bt in blocked_tasks if bt["status"] == "pending"]
                for bt in blocked_tasks:
                    if self._is_task_unblocked(bt, db):
                        newly_unblocked.append({"id": bt["id"], "title": bt["title"]})
                db.commit()
        if error_response:
            _respond_json(self, error_response[0], error_response[1])
            return
        for ub in newly_unblocked:
            log_activity(None, "task_unblocked", f"#{ub['id']}: {ub['title']} (dependency #{task_id} completed)")
        log_activity(peer_id, "task_completed", f"#{task_id}: {task['title']}")
        result = {"ok": True, "newly_unblocked": newly_unblocked}
        _respond_json(self, result)

    # ---- Shared Memory ----

    def _handle_get_memory(self, params: dict) -> None:
        key = params.get("key", [None])[0]
        mem_type = params.get("type", [None])[0]
        if key:
            with db_lock:
                row = db.execute("SELECT * FROM shared_memory WHERE key = ?", (key,)).fetchone()
            if row:
                _respond_json(self, {"key": row["key"], "value": row["value"], "peer_id": row["peer_id"],
                                     "updated_at": row["updated_at"], "type": row["type"],
                                     "version": row["version"], "confidence": row["confidence"],
                                     "supersedes": row["supersedes"]})
            else:
                _respond_json(self, {"error": f"key '{key}' not found"}, 404)
        else:
            with db_lock:
                if mem_type:
                    rows = db.execute("SELECT * FROM shared_memory WHERE type = ? ORDER BY updated_at DESC", (mem_type,)).fetchall()
                else:
                    rows = db.execute("SELECT * FROM shared_memory ORDER BY updated_at DESC").fetchall()
            result = [{"key": r["key"], "value": r["value"], "peer_id": r["peer_id"],
                       "updated_at": r["updated_at"], "type": r["type"],
                       "version": r["version"], "confidence": r["confidence"]} for r in rows]
            _respond_json(self, {"memory": result})

    def _handle_post_memory(self, body: dict) -> None:
        key = body.get("key", "")
        value = body.get("value", "")
        peer_id = body.get("peer_id", "")
        if not all([key, value, peer_id]):
            _respond_json(self, {"error": "key, value, peer_id required"}, 400)
            return
        value = value[:50000]  # cap at 50KB
        mem_type = body.get("type", "fact")
        confidence = body.get("confidence", "high")
        supersedes = body.get("supersedes", "")
        if mem_type not in ("decision", "fact", "constraint", "artifact"):
            mem_type = "fact"
        if confidence not in ("high", "medium", "low"):
            confidence = "high"
        now = time.time()
        with db_lock:
            # Get current version
            existing = db.execute("SELECT version FROM shared_memory WHERE key = ?", (key,)).fetchone()
            new_version = (existing["version"] + 1) if existing else 1
            # Save to history before overwriting
            if existing:
                old = db.execute("SELECT * FROM shared_memory WHERE key = ?", (key,)).fetchone()
                db.execute(
                    "INSERT INTO shared_memory_history (key, value, peer_id, updated_at, type, version, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (old["key"], old["value"], old["peer_id"], old["updated_at"], old["type"], old["version"], old["confidence"]),
                )
            # Upsert with version bump
            db.execute(
                """INSERT INTO shared_memory (key, value, peer_id, updated_at, type, version, confidence, supersedes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, peer_id=excluded.peer_id, updated_at=excluded.updated_at,
                     type=excluded.type, version=excluded.version, confidence=excluded.confidence,
                     supersedes=excluded.supersedes""",
                (key, value, peer_id, now, mem_type, new_version, confidence, supersedes),
            )
            db.commit()
        log_activity(peer_id, "memory_set", f"key={key} v{new_version} type={mem_type}")
        _respond_json(self, {"ok": True, "version": new_version})

    # ---- Pause / Resume / Ping / Config ----

    def _handle_pause(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        with db_lock:
            peer = db.execute("SELECT id, status FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        with db_lock:
            db.execute("UPDATE peers SET paused = 1 WHERE id = ?", (peer_id,))
            db.commit()
        log_activity(peer_id, "paused", "Agent paused")
        _respond_json(self, {"ok": True, "peer_id": peer_id, "paused": True})

    def _handle_resume(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        with db_lock:
            peer = db.execute("SELECT id, status FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        with db_lock:
            db.execute("UPDATE peers SET paused = 0 WHERE id = ?", (peer_id,))
            db.commit()
        log_activity(peer_id, "resumed", "Agent resumed")
        _respond_json(self, {"ok": True, "peer_id": peer_id, "paused": False})

    def _handle_ping(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        with db_lock:
            peer = db.execute("SELECT * FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        elapsed = time.time() - (peer["last_heartbeat"] or 0)
        possibly_stuck = peer["status"] == "active" and elapsed > 20
        # Find current task
        with db_lock:
            current_task = db.execute(
                "SELECT id, title FROM tasks WHERE assigned_to = ? AND status = 'in_progress'",
                (peer_id,),
            ).fetchone()
        _respond_json(self, {
            "ok": True,
            "peer_id": peer_id,
            "status": peer["status"],
            "paused": bool(peer["paused"]),
            "last_heartbeat": peer["last_heartbeat"],
            "seconds_since_heartbeat": round(elapsed, 1),
            "possibly_stuck": possibly_stuck,
            "current_task": {"id": current_task["id"], "title": current_task["title"]} if current_task else None,
            "git_branch": peer["git_branch"],
            "git_dirty_files": peer["git_dirty_files"],
            "git_last_commit": peer["git_last_commit"],
        })

    def _handle_ping_all(self, body: dict) -> None:
        with db_lock:
            peers = db.execute("SELECT * FROM peers WHERE status = 'active' AND id != 'command-center'").fetchall()
        results = []
        for peer in peers:
            elapsed = time.time() - (peer["last_heartbeat"] or 0)
            with db_lock:
                current_task = db.execute(
                    "SELECT id, title FROM tasks WHERE assigned_to = ? AND status = 'in_progress'",
                    (peer["id"],),
                ).fetchone()
            results.append({
                "peer_id": peer["id"],
                "role": peer["role"],
                "status": peer["status"],
                "paused": bool(peer["paused"]),
                "seconds_since_heartbeat": round(elapsed, 1),
                "possibly_stuck": elapsed > 20,
                "current_task": {"id": current_task["id"], "title": current_task["title"]} if current_task else None,
                "git_branch": peer["git_branch"],
            })
        _respond_json(self, {"ok": True, "peers": results})

    def _handle_config(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        poll_interval_ms = body.get("poll_interval_ms")
        if poll_interval_ms is not None:
            # Clamp to range 1000-60000 (1s to 60s)
            try:
                poll_interval_ms = max(1000, min(60000, int(poll_interval_ms)))
            except (ValueError, TypeError):
                _respond_json(self, {"error": "poll_interval_ms must be a number"}, 400)
                return
            with db_lock:
                db.execute("UPDATE peers SET poll_interval_ms = ? WHERE id = ?", (poll_interval_ms, peer_id))
                db.commit()
            log_activity(peer_id, "config_changed", f"poll_interval_ms={poll_interval_ms}")
        _respond_json(self, {"ok": True, "peer_id": peer_id, "poll_interval_ms": poll_interval_ms})

    # ---- Git Awareness (Phase 3) ----

    def _handle_heartbeat(self, body: dict) -> None:
        peer_id = body.get("id", "")
        if not peer_id:
            _respond_json(self, {"error": "id is required"}, 400)
            return
        now = time.time()
        # Git state from heartbeat (Phase 3)
        git_branch = body.get("git_branch", "")
        git_dirty_files = body.get("git_dirty_files", "")
        git_last_commit = body.get("git_last_commit", "")
        # Telemetry from heartbeat
        tokens_used = body.get("tokens_used")
        tool_calls = body.get("tool_calls_count")
        errors = body.get("errors_count")
        # Safe numeric parsing for telemetry
        try:
            if tokens_used is not None:
                tokens_used = int(tokens_used)
            if tool_calls is not None:
                tool_calls = int(tool_calls)
            if errors is not None:
                errors = int(errors)
        except (ValueError, TypeError):
            pass  # ignore bad telemetry, don't reject heartbeat
        with db_lock:
            update_sql = """UPDATE peers SET last_heartbeat = ?, status = 'active',
                   git_branch = ?, git_dirty_files = ?, git_last_commit = ?"""
            update_params = [now, git_branch, git_dirty_files, git_last_commit]
            if tokens_used is not None and isinstance(tokens_used, int):
                update_sql += ", tokens_used = ?"
                update_params.append(tokens_used)
            if tool_calls is not None and isinstance(tool_calls, int):
                update_sql += ", tool_calls_count = ?"
                update_params.append(tool_calls)
            if errors is not None and isinstance(errors, int):
                update_sql += ", errors_count = ?"
                update_params.append(errors)
            update_sql += " WHERE id = ?"
            update_params.append(peer_id)
            db.execute(update_sql, update_params)
            db.commit()

            # Conflict detection: check for overlapping dirty files across peers
            if git_dirty_files:
                dirty_set = set(f.strip() for f in git_dirty_files.split(",") if f.strip())
                other_peers = db.execute(
                    "SELECT id, git_dirty_files FROM peers WHERE status = 'active' AND id != ? AND git_dirty_files != ''",
                    (peer_id,),
                ).fetchall()
                for other in other_peers:
                    other_dirty = set(f.strip() for f in other["git_dirty_files"].split(",") if f.strip())
                    overlap = dirty_set & other_dirty
                    if overlap:
                        log_activity(
                            peer_id, "conflict_warning",
                            f"Overlapping dirty files with {other['id']}: {', '.join(sorted(overlap)[:5])}"
                        )

            # Return pause state + poll interval + budget so MCP server can adjust
            peer = db.execute("SELECT paused, poll_interval_ms, token_budget, tokens_used FROM peers WHERE id = ?", (peer_id,)).fetchone()

        # Only log every Nth heartbeat to reduce noise
        with self._heartbeat_lock:
            count = self._heartbeat_counts.get(peer_id, 0) + 1
            self._heartbeat_counts[peer_id] = count
        if count % HEARTBEAT_LOG_INTERVAL == 0:
            log_activity(peer_id, "heartbeat", "")

        response = {"ok": True}
        if peer:
            response["paused"] = bool(peer["paused"])
            response["poll_interval_ms"] = peer["poll_interval_ms"]
            response["token_budget"] = peer["token_budget"]
            response["tokens_used"] = peer["tokens_used"]
            if peer["token_budget"] is not None and peer["tokens_used"] >= peer["token_budget"]:
                response["budget_exceeded"] = True
        _respond_json(self, response)

    # ---- Phase 4: Budget Caps ----

    def _handle_budget(self, body: dict) -> None:
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        token_budget = body.get("token_budget")  # None = unlimited
        tokens_used = body.get("tokens_used")  # optional: reset/set usage
        with db_lock:
            peer = db.execute("SELECT id FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        try:
            if token_budget is not None:
                token_budget = int(token_budget)
            if tokens_used is not None:
                tokens_used = int(tokens_used)
        except (ValueError, TypeError):
            _respond_json(self, {"error": "token_budget and tokens_used must be numbers"}, 400)
            return
        with db_lock:
            if token_budget is not None:
                db.execute("UPDATE peers SET token_budget = ? WHERE id = ?", (token_budget, peer_id))
            if tokens_used is not None:
                db.execute("UPDATE peers SET tokens_used = ? WHERE id = ?", (tokens_used, peer_id))
            db.commit()
            updated = db.execute("SELECT token_budget, tokens_used FROM peers WHERE id = ?", (peer_id,)).fetchone()
        log_activity(peer_id, "budget_set", f"budget={updated['token_budget']} used={updated['tokens_used']}")
        _respond_json(self, {
            "ok": True, "peer_id": peer_id,
            "token_budget": updated["token_budget"],
            "tokens_used": updated["tokens_used"],
            "budget_exceeded": (updated["token_budget"] is not None and
                                updated["tokens_used"] >= updated["token_budget"]),
        })

    # ---- Phase 7: Runs ----

    def _handle_get_runs(self, params: dict) -> None:
        status_filter = params.get("status", [None])[0]
        with db_lock:
            if status_filter:
                runs = db.execute("SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC", (status_filter,)).fetchall()
            else:
                runs = db.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        result = [{"id": r["id"], "name": r["name"], "goal": r["goal"],
                   "success_criteria": r["success_criteria"], "status": r["status"],
                   "created_by": r["created_by"], "created_at": r["created_at"]} for r in runs]
        _respond_json(self, {"runs": result})

    def _handle_get_run(self, run_id: str) -> None:
        with db_lock:
            run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            _respond_json(self, {"error": f"run '{run_id}' not found"}, 404)
            return
        with db_lock:
            tasks = db.execute("SELECT id, title, status, assigned_to FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            msg_count = db.execute("SELECT COUNT(*) as c FROM messages WHERE run_id = ?", (run_id,)).fetchone()["c"]
        _respond_json(self, {
            "run": {"id": run["id"], "name": run["name"], "goal": run["goal"],
                    "success_criteria": run["success_criteria"], "status": run["status"],
                    "created_by": run["created_by"], "created_at": run["created_at"]},
            "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"], "assigned_to": t["assigned_to"]} for t in tasks],
            "message_count": msg_count,
        })

    def _handle_get_run_summary(self, run_id: str) -> None:
        """Return a comprehensive summary of a run for session resume."""
        with db_lock:
            run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            _respond_json(self, {"error": f"run '{run_id}' not found"}, 404)
            return
        with db_lock:
            tasks = db.execute(
                "SELECT id, title, description, status, priority, assigned_to, artifacts, blocked_by "
                "FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            memory = db.execute("SELECT * FROM shared_memory ORDER BY updated_at DESC").fetchall()
            messages = db.execute(
                "SELECT sender_id, recipient_id, category, content, created_at "
                "FROM messages WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)
            ).fetchall()

        task_list = []
        for t in tasks:
            artifacts_str = t["artifacts"] or ""
            try:
                artifacts = json.loads(artifacts_str) if artifacts_str else {}
            except (json.JSONDecodeError, TypeError):
                artifacts = {}
            task_list.append({
                "id": t["id"], "title": t["title"], "description": t["description"],
                "status": t["status"], "priority": t["priority"],
                "assigned_to": t["assigned_to"], "artifacts": artifacts,
                "blocked_by": t["blocked_by"],
            })

        memory_list = [
            {"key": m["key"], "value": m["value"], "type": m["type"],
             "confidence": m["confidence"], "updated_at": m["updated_at"]}
            for m in memory
        ]

        message_list = [
            {"sender_id": m["sender_id"], "recipient_id": m["recipient_id"],
             "category": m["category"], "content": m["content"],
             "created_at": m["created_at"]}
            for m in messages
        ]

        _respond_json(self, {
            "run": {"id": run["id"], "name": run["name"], "goal": run["goal"],
                    "success_criteria": run["success_criteria"], "status": run["status"],
                    "created_by": run["created_by"], "created_at": run["created_at"]},
            "tasks": task_list,
            "memory": memory_list,
            "recent_messages": message_list,
        })

    def _handle_post_run(self, body: dict) -> None:
        run_id = body.get("id", "")
        name = body.get("name", "")
        if not all([run_id, name]):
            _respond_json(self, {"error": "id and name are required"}, 400)
            return
        # Allow update status of existing run
        status = body.get("status")
        with db_lock:
            existing = db.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if existing:
            if status:
                with db_lock:
                    db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
                    db.commit()
                log_activity(None, "run_updated", f"run={run_id} status={status}")
                _respond_json(self, {"ok": True, "run_id": run_id, "status": status})
                return
            else:
                _respond_json(self, {"error": f"run '{run_id}' already exists"}, 409)
                return
        now = time.time()
        with db_lock:
            db.execute(
                "INSERT INTO runs (id, name, goal, success_criteria, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, name, body.get("goal", ""), body.get("success_criteria", ""),
                 body.get("created_by", ""), now),
            )
            db.commit()
        log_activity(body.get("created_by"), "run_created", f"run={run_id}: {name}")
        _respond_json(self, {"ok": True, "run_id": run_id})

    # ---- Phase 6: Memory History ----

    def _handle_get_memory_history(self, params: dict) -> None:
        key = params.get("key", [None])[0]
        if not key:
            _respond_json(self, {"error": "key parameter required"}, 400)
            return
        with db_lock:
            # Include current version + all previous versions
            current = db.execute(
                "SELECT * FROM shared_memory WHERE key = ?", (key,)
            ).fetchone()
            history = db.execute(
                "SELECT * FROM shared_memory_history WHERE key = ? ORDER BY version DESC LIMIT 50",
                (key,),
            ).fetchall()
        result = []
        if current:
            result.append({"key": current["key"], "value": current["value"],
                           "peer_id": current["peer_id"], "updated_at": current["updated_at"],
                           "type": current["type"], "version": current["version"],
                           "confidence": current["confidence"]})
        result.extend([{"key": h["key"], "value": h["value"], "peer_id": h["peer_id"],
                   "updated_at": h["updated_at"], "type": h["type"],
                   "version": h["version"], "confidence": h["confidence"]} for h in history])
        _respond_json(self, {"history": result})

    # ---- Kill / Unregister / Bulk ----

    def _handle_kill_peer(self, body: dict) -> None:
        """Kill a peer: mark dead, release locks, unassign tasks, send shutdown message."""
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        with db_lock:
            peer = db.execute("SELECT id, status FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        with db_lock:
            db.execute("UPDATE peers SET status = 'dead' WHERE id = ?", (peer_id,))
            released = db.execute("DELETE FROM file_locks WHERE peer_id = ?", (peer_id,)).rowcount
            unassigned = db.execute(
                "UPDATE tasks SET status = 'pending', assigned_to = NULL WHERE assigned_to = ? AND status = 'in_progress'",
                (peer_id,),
            ).rowcount
            db.execute(
                "INSERT INTO messages (sender_id, recipient_id, category, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (peer_id, peer_id, "alert", "[KILL] Terminated by command center.", time.time()),
            )
            db.commit()
        log_activity(peer_id, "killed", f"locks_released={released} tasks_unassigned={unassigned}")
        _respond_json(self, {"ok": True, "peer_id": peer_id, "locks_released": released, "tasks_unassigned": unassigned})

    def _handle_unregister(self, body: dict) -> None:
        """Hard-delete a peer from the database entirely."""
        peer_id = body.get("peer_id", "")
        if not peer_id:
            _respond_json(self, {"error": "peer_id is required"}, 400)
            return
        with db_lock:
            peer = db.execute("SELECT id FROM peers WHERE id = ?", (peer_id,)).fetchone()
        if not peer:
            _respond_json(self, {"error": f"peer '{peer_id}' not found"}, 404)
            return
        with db_lock:
            db.execute("DELETE FROM file_locks WHERE peer_id = ?", (peer_id,))
            db.execute("DELETE FROM conversations WHERE peer_id = ?", (peer_id,))
            db.execute("UPDATE tasks SET assigned_to = NULL, status = 'pending' WHERE assigned_to = ? AND status = 'in_progress'", (peer_id,))
            db.execute("DELETE FROM peers WHERE id = ?", (peer_id,))
            db.commit()
        log_activity(peer_id, "unregistered", "Hard-deleted from database")
        _respond_json(self, {"ok": True, "peer_id": peer_id})

    def _handle_pause_all(self, body: dict) -> None:
        with db_lock:
            count = db.execute("UPDATE peers SET paused = 1 WHERE status = 'active'").rowcount
            db.commit()
        log_activity(None, "pause_all", f"Paused {count} peers")
        _respond_json(self, {"ok": True, "paused_count": count})

    def _handle_resume_all(self, body: dict) -> None:
        with db_lock:
            count = db.execute("UPDATE peers SET paused = 0 WHERE status = 'active'").rowcount
            db.commit()
        log_activity(None, "resume_all", f"Resumed {count} peers")
        _respond_json(self, {"ok": True, "resumed_count": count})

    # ---- Shutdown ----

    def _handle_shutdown(self, body: dict) -> None:
        requester = body.get("requester", "")
        role = _get_peer_role(requester)
        if role != "architect":
            _respond_json(self, {"error": "architect-only endpoint"}, 403)
            return
        log_activity(requester, "shutdown", "Broker shutdown requested")
        _respond_json(self, {"ok": True, "message": "shutting down"})
        # Schedule shutdown after response is sent
        def _shutdown():
            sys.stderr.write("[broker] Shutting down by architect request.\n")
            os._exit(0)
        t = Timer(0.5, _shutdown)
        t.daemon = True
        t.start()

    def _handle_spawn(self, body: dict) -> None:
        """Spawn a new Claude Code session (architect or worker) in a terminal."""
        role = body.get("role", "worker").strip()
        if role not in ("architect", "worker"):
            _respond_json(self, {"error": "role must be 'architect' or 'worker'"}, 400)
            return

        peer_id = body.get("peer_id", "").strip()
        if not peer_id:
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            peer_id = f"{role}-{suffix}"

        # Validate peer_id format
        err = _validate_id(peer_id, "peer_id")
        if err:
            _respond_json(self, {"error": err}, 400)
            return

        working_dir = body.get("working_dir", "").strip() or os.path.dirname(os.path.abspath(__file__))

        env = os.environ.copy()
        env["PEER_ROLE"] = role
        env["PEER_ID"] = peer_id

        # Write config file so the MCP server (spawned by Claude Code) can
        # pick up the intended peer_id and role even when env vars don't
        # propagate through Claude Code's own process launcher.
        try:
            config_path = os.path.join(os.path.expanduser("~"), ".c2-lattice-next.json")
            with open(config_path, "w", encoding="utf-8") as cf:
                json.dump({"peer_id": peer_id, "role": role}, cf)
        except OSError:
            pass  # best-effort — env vars may still work

        try:
            # Write system prompt to temp file for --append-system-prompt-file
            if role == "architect":
                init_msg = (
                    f"You are {peer_id}, an C2 Lattice architect agent. "
                    "CRITICAL: You MUST use c2-lattice MCP tools to coordinate ALL work. "
                    "Do NOT use the Agent tool or local subagents for parallel work. "
                    "Your workflow: "
                    "(1) Call list_peers FIRST to register with the broker. "
                    "(2) Break work into tasks using create_task with dependencies. "
                    "(3) Spawn 2-3 workers using spawn_worker. "
                    "(4) Wait 20 seconds for workers to boot, then send each one their assignment via send_message. "
                    "(5) Monitor with list_tasks and check_messages periodically. "
                    "(6) When all tasks complete, send a summary to command-center via send_message. "
                    "Start every session by calling list_peers."
                )
            else:
                init_msg = (
                    f"You are {peer_id}, an C2 Lattice worker agent. "
                    "CRITICAL: Call list_peers FIRST to register. Then call list_tasks to find work. "
                    "Claim a task with claim_task, do the work, then complete_task with artifacts. "
                    "After completing, call list_tasks for more work. If blocked, call raise_blocker. "
                    "Check check_messages regularly for instructions from the architect."
                )
            prompt_file = os.path.join(tempfile.gettempdir(), f"c2-lattice-{peer_id}.txt")
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(init_msg)

            if sys.platform == "win32":
                cmd_line = f'title C2-{peer_id} && set PEER_ROLE={role} && set PEER_ID={peer_id} && claude --append-system-prompt-file "{prompt_file}"'
                if shutil.which("wt"):
                    subprocess.Popen(
                        ["wt", "-w", "0", "nt", "--title", f"C2-{peer_id}", "cmd", "/k", cmd_line],
                        env=env,
                    )
                else:
                    subprocess.Popen(
                        ["cmd", "/c", "start", f"C2-{peer_id}", "cmd", "/k", cmd_line],
                        env=env,
                        creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
                    )
            elif sys.platform == "darwin":
                esc_dir = working_dir.replace('"', '\\"')
                script = f'tell application "Terminal" to do script "cd \\"{esc_dir}\\" && PEER_ROLE={role} PEER_ID={peer_id} claude --append-system-prompt-file \\"{prompt_file}\\""'
                subprocess.Popen(["osascript", "-e", script])
            else:
                for term_cmd in [
                    ["x-terminal-emulator", "-e"],
                    ["gnome-terminal", "--"],
                    ["xterm", "-e"],
                ]:
                    if shutil.which(term_cmd[0]):
                        subprocess.Popen(
                            term_cmd + ["bash", "-c", f"cd '{working_dir}' && PEER_ROLE={role} PEER_ID={peer_id} exec claude --append-system-prompt-file '{prompt_file}'"],
                            env=env, start_new_session=True,
                        )
                        break
                else:
                    _respond_json(self, {"error": "No terminal emulator found"}, 500)
                    return

            log_activity(None, "spawn", f"Spawned {role} as {peer_id}")
            _respond_json(self, {"ok": True, "peer_id": peer_id, "message": f"Launched {role} '{peer_id}'"})
        except Exception as e:
            _respond_json(self, {"error": f"Failed to spawn: {e}"}, 500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global _system_token

    def _shutdown_handler(signum, frame):
        sys.stderr.write(f"\n[broker] Shutting down (signal {signum})...\n")
        log_activity(None, "broker_stopped", f"signal {signum}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    # Register command-center as a system peer (satisfies FK constraints for dashboard-sent messages)
    # Uses 'system' role to avoid blocking architect registration
    with db_lock:
        db.execute(
            """INSERT INTO peers (id, role, pid, working_dir, summary, last_heartbeat, registered_at, status)
               VALUES ('command-center', 'system', NULL, '', 'Browser command center', ?, ?, 'active')
               ON CONFLICT(id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat, status = 'active'""",
            (time.time(), time.time()),
        )
        db.commit()

    # Generate system token for dashboard/command-center
    _system_token = generate_token("command-center", "system")

    # Start dead-peer sweeper
    sweeper = Timer(DEAD_CLEAN_INTERVAL, sweep_dead_peers)
    sweeper.daemon = True
    sweeper.start()

    log_activity(None, "broker_started", f"port={PORT} db={DB_PATH}")

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        request_queue_size = 20
        timeout = 30  # socket accept timeout

    server = ThreadedHTTPServer(("127.0.0.1", PORT), BrokerHandler)
    server.timeout = 30
    sys.stderr.write(f"[broker] C2 Lattice broker listening on 127.0.0.1:{PORT}\n")
    sys.stderr.write(f"[broker] Database: {DB_PATH}\n")
    sys.stderr.write(f"[broker] Dashboard: http://127.0.0.1:{PORT}/dashboard\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[broker] Shutting down.\n")
        log_activity(None, "broker_stopped", "KeyboardInterrupt")
        server.server_close()


if __name__ == "__main__":
    main()
