# Security Policy

## Security Model

C2 Lattice is designed for local multi-agent coordination on a single machine. The security model reflects this scope:

- **Localhost only.** The broker binds to `127.0.0.1` and does not accept external connections.
- **HMAC-SHA256 authentication.** Every peer receives a signed token at registration. All subsequent requests are validated against this token.
- **Role-based access control.** Architect and worker roles have different permissions. Privileged operations (spawn, pause, kill, broadcast) require architect or system role.
- **Identity enforcement.** The peer ID in request bodies must match the token's subject claim. Workers cannot impersonate other peers.
- **Content filtering.** Messages are scanned for prompt injection patterns (tool_use blocks, function-call JSON, base64 payloads, data URIs, long file paths) and rejected if matched.
- **Rate limiting.** 10 messages per 60-second window per peer, with automatic pause after 20 cumulative rejections.
- **Input validation.** All identifiers are checked against length limits and character whitelists. Request bodies are capped at 100KB.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately:

1. **Email:** Send details to the repository maintainers via the contact information in the GitHub profile.
2. **Do not** open a public GitHub issue for security vulnerabilities.
3. **Include** a description of the vulnerability, steps to reproduce, and potential impact.
4. **Allow** up to 72 hours for an initial response.

We will acknowledge receipt, investigate, and coordinate a fix before any public disclosure.

## Scope

### In Scope

- Authentication bypass or token forgery
- Role-based access control escalation
- Content filter bypass that enables prompt injection
- Rate limiter bypass
- SQLite injection
- Denial of service against the broker
- Identity spoofing (sending messages as another peer)
- File lock manipulation

### Out of Scope

- Attacks requiring access to the host machine (the broker is localhost-only by design)
- Social engineering of the human operator
- Denial of service via resource exhaustion on the host OS
- Vulnerabilities in Python itself or the operating system

## Security Features

| Feature | Implementation |
|---|---|
| Token auth | HMAC-SHA256 signed tokens with peer ID, role, and timestamp |
| Role-based access | Architect, worker, and system roles with endpoint-level checks |
| Content filtering | 5 regex patterns blocking tool_use, function-call JSON, base64, data URIs, long paths |
| Rate limiting | Sliding window (60s, 10 max) per peer, in-memory |
| Identity enforcement | Body peer_id must match token subject for non-privileged roles |
| Input validation | ID length (64 chars), key length (256 chars), path length (512 chars), character whitelist |
| Request size limits | 100KB max body, 10KB max message content |
| Auto-pause | Peers auto-paused after 20 cumulative rejections |
| Dead peer cleanup | 15-second sweep interval, task reassignment, lock release |
| Unicode normalization | NFKC normalization before content filtering to prevent homoglyph bypass |

## Audits

This project has undergone:

- **Dual AI audit** (Claude + Codex) with all identified issues fixed
- **Stress testing** under concurrent load (235+ test cases)
- **Chaos testing** with fault injection and recovery validation
- **Abuse scenario testing** (58 scenarios, zero crashes)
