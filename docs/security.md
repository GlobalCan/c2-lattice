# Security Model

## Overview

C2 Lattice is a localhost-only coordination system. Its security model is designed to prevent accidental misuse and prompt injection between agent sessions, not to defend against a hostile local attacker with shell access.

## HMAC-SHA256 Token Design

### Token Generation

Tokens are generated at peer registration:

```
payload = {"sub": peer_id, "role": role, "iat": unix_timestamp}
payload_b64 = base64url_encode(json(payload))
signature = hmac_sha256(broker_secret, payload_b64)
token = payload_b64 + "." + signature
```

### Broker Secret

- Generated randomly at startup: `os.urandom(32).hex()` (64 hex chars, 256 bits)
- Can be set via `C2_LATTICE_SECRET` environment variable for persistence across restarts
- Stored in memory only; never written to disk or included in responses

### Token Validation

On every authenticated request:

1. Extract `Authorization: Bearer <token>` header
2. Split token into `payload_b64` and `signature`
3. Recompute HMAC-SHA256 of `payload_b64` using the broker secret
4. Compare signatures using `hmac.compare_digest` (constant-time comparison)
5. Decode payload to extract `sub` (peer ID) and `role`

## Role-Based Access Matrix

| Endpoint | Method | No Auth | Worker | Architect | System |
|---|---|---|---|---|---|
| `/register` | POST | Yes | - | - | - |
| `/health` | GET | Yes | - | - | - |
| `/dashboard` | GET | Yes | - | - | - |
| `/dashboard/data` | GET | Yes | - | - | - |
| `/dashboard/token` | GET | Yes | - | - | - |
| `/heartbeat` | POST | - | Yes | Yes | Yes |
| `/send` | POST | - | Yes | Yes | Yes |
| `/summary` | POST | - | Yes | Yes | Yes |
| `/conversation` | POST | - | Yes | Yes | Yes |
| `/lock` | POST | - | Yes | Yes | Yes |
| `/unlock` | POST | - | Yes | Yes | Yes |
| `/tasks` | POST | - | Yes | Yes | Yes |
| `/tasks/claim` | POST | - | Yes | Yes | Yes |
| `/tasks/complete` | POST | - | Yes | Yes | Yes |
| `/memory` | POST | - | Yes | Yes | Yes |
| `/peers` | GET | - | Yes | Yes | Yes |
| `/messages/{id}` | GET | - | Yes | Yes | Yes |
| `/tasks` | GET | - | Yes | Yes | Yes |
| `/memory` | GET | - | Yes | Yes | Yes |
| `/locks` | GET | - | Yes | Yes | Yes |
| `/log` | GET | - | No | Yes | Yes |
| `/pause` | POST | - | No | Yes | Yes |
| `/resume` | POST | - | No | Yes | Yes |
| `/spawn` | POST | - | No | Yes | Yes |
| `/kill-peer` | POST | - | No | Yes | Yes |
| `/config` | POST | - | No | Yes | Yes |
| `/budget` | POST | - | No | Yes | Yes |
| `/pause-all` | POST | - | No | Yes | Yes |
| `/resume-all` | POST | - | No | Yes | Yes |
| `/shutdown` | POST | - | No | No | Yes |

## Content Filtering

All message content is filtered after NFKC Unicode normalization. The following patterns cause message rejection with HTTP 422:

### Pattern 1: Tool Use XML Tags
```regex
<tool_use>|</tool_use>|<tool_call>|</tool_call>|<function_calls>|</function_calls>
```
**Purpose:** Blocks XML-formatted tool invocation blocks that could trick an LLM into executing embedded tool calls from message content.

### Pattern 2: Function Call JSON
```regex
"function"\s*:\s*\{|"tool_calls"\s*:\s*\[|"name"\s*:\s*"[^"]+"\s*,\s*"arguments"
```
**Purpose:** Blocks JSON structures that resemble OpenAI-style function call payloads, preventing prompt injection via structured tool invocation.

### Pattern 3: Deep File Paths
```regex
(?:[A-Za-z]:\\|/)(?:[^\s\\/:*?"<>|]+[\\/]){3,}[^\s\\/:*?"<>|]*
```
**Purpose:** Blocks file paths with 3+ directory segments, preventing path traversal payloads and directory enumeration attempts in messages.

### Pattern 4: Long Base64 Strings
```regex
[A-Za-z0-9+/]{100,}={0,3}
```
**Purpose:** Blocks base64-encoded content longer than 100 characters, preventing encoded payloads that could bypass text-based filtering.

### Pattern 5: Data URIs
```regex
data:[a-zA-Z0-9/+.-]+;base64,
```
**Purpose:** Blocks data URIs with base64 encoding, preventing embedded binary content (images, scripts) in messages.

## Rate Limiting

- **Window:** 60 seconds (sliding)
- **Limit:** 10 messages per window per peer
- **Storage:** In-memory dictionary (resets on broker restart)
- **Enforcement:** Old entries pruned on each check; new entry added if under limit
- **Response:** HTTP 429 with error message when exceeded
- **Escalation:** After 20 cumulative rejections, the peer is auto-paused

## Identity Enforcement

For non-privileged roles (worker), the broker enforces identity consistency:

1. Extract `peer_id`, `sender_id`, or `id` from the request body
2. Compare against the `sub` claim in the Bearer token
3. If they do not match, reject with HTTP 403

This prevents a worker from sending messages as another peer or modifying another peer's data. Architect and system roles are exempt from this check to allow management operations.

## Threat Model

### Scope

C2 Lattice is a **localhost-only** system. The broker binds to `127.0.0.1` and is not accessible from the network. The threat model assumes:

- The host machine is trusted
- The human operator is trusted
- Agent sessions may produce untrustworthy output (LLM hallucinations, prompt injection)

### Mitigated Threats

| Threat | Mitigation |
|---|---|
| Agent sends tool invocation via message | Content filter blocks tool_use/function_call patterns |
| Agent impersonates another agent | Identity enforcement matches body peer_id to token subject |
| Agent floods the broker | Rate limiting (10/60s), auto-pause after 20 rejections |
| Agent sends encoded payload | Content filter blocks base64 and data URIs |
| Dead agent holds locks | Dead peer sweeper releases locks every 15 seconds |
| Dead agent blocks tasks | Dead peer sweeper reassigns in_progress tasks |
| Homoglyph bypass of content filter | NFKC Unicode normalization before filtering |
| Oversized request body | 100KB request limit, 10KB message limit |

### OWASP Alignment

The content filtering and identity enforcement align with [OWASP ASAI07 (Agentic AI Security)](https://owasp.org/), specifically addressing inter-agent communication integrity and preventing tool invocation injection.

## Audit History

| Audit | Scope | Result |
|---|---|---|
| Claude security audit | Auth, content filter, rate limiting, input validation | All findings fixed |
| Codex security audit | Same scope, independent review | All findings fixed |
| Stress test suite | Concurrent load, race conditions, edge cases | 235+ tests passing |
| Chaos test suite | Fault injection, broker recovery, state corruption | All scenarios pass |
| Abuse scenario suite | 58 abuse patterns (malformed input, injection, overflow) | Zero crashes |
