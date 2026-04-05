# Changelog

All notable changes to C2 Lattice are documented in this file.

## [4.2.0] - 2026

### Added
- Dashboard extracted to standalone HTML file (`dashboard.html`) with client-side rendering
- Graceful shutdown via SIGINT/SIGTERM signal handlers
- Offline peer detection with 3-second heartbeat interval
- `/dashboard/data` API endpoint for full dashboard state as JSON
- `/dashboard/token` endpoint for dashboard authentication
- Sparkline activity visualization (24 buckets over 6 hours)
- Attention items panel (blockers, reviews, paused agents, claimable tasks)

### Changed
- Heartbeat interval reduced from 10s to 3s for faster peer detection
- Dead peer sweeper runs every 15 seconds with 15-second timeout
- Dashboard refreshes via client-side polling instead of server-rendered HTML

## [4.1.0] - 2026

### Added
- HMAC-SHA256 token authentication issued at registration
- Role-based access control (architect, worker, system roles)
- Identity enforcement: request body peer_id must match token subject
- `spawn_worker` tool for architect to open new worker sessions
- Privileged endpoint protection (pause, resume, kill, spawn, config, budget)
- System-only endpoint protection (shutdown)

### Changed
- All non-public endpoints now require Bearer token authentication
- Broadcast restricted to architect role only

## [4.0.0] - 2026

### Added
- Task DAGs with `blocked_by` dependencies and auto-unblock on completion
- Shared memory with versioning, types (decision/fact/constraint/artifact), and confidence levels
- File locks with automatic release on peer death
- Escalation routing: `raise_blocker` and `request_review` tools
- Auto-escalation of blocker messages to architect
- Auto-broadcast of error messages to all peers
- Interactive dashboard with bento grid layout
- Conversation logging per peer
- Run tracking with goals and success criteria
- PID liveness checking for faster dead peer detection

### Changed
- SQLite schema expanded with tasks, shared_memory, file_locks, conversations, runs tables
- Dead peer sweeper now reassigns in-progress tasks and releases file locks

## [3.0.0] - 2026

### Added
- Bento grid dashboard layout
- Budget caps (token budgets per peer)
- Telemetry counters (tool calls, errors, rejections per peer)
- Denial tracking with auto-pause after 20 rejections
- Tool risk metadata
- Git state collection (branch, dirty files, last commit) sent with heartbeat

### Changed
- Dashboard redesigned with card-based peer display

## [2.0.0] - 2026

### Added
- Content filtering with 5 regex patterns (tool_use, function-call JSON, base64, data URIs, long paths)
- Rate limiting (10 messages per 60s per peer)
- Dead peer sweeper on configurable timer
- Heartbeat system for liveness detection
- Unicode normalization (NFKC) before content filtering

### Changed
- Messages validated against size limits (10KB)
- Request bodies capped at 100KB

## [1.0.0] - 2026

### Added
- Peer discovery and registration
- Message passing between peers
- Basic browser dashboard
- SQLite-backed broker on localhost
- MCP server with stdio JSON-RPC 2.0 transport
- Threaded HTTP server (ThreadingMixIn)
