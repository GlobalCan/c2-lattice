# Contributing to C2 Lattice

Thank you for your interest in contributing. This document covers the process for reporting bugs, submitting changes, and maintaining code quality.

## Reporting Bugs

Open an issue on GitHub using the **Bug Report** template. Include:

- Python version and OS
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output (broker stderr, MCP server stderr)

For security vulnerabilities, **do not open a public issue**. See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

## Submitting Changes

1. **Fork** the repository and create a branch from `main`.
2. **Make your changes.** Follow the code style guidelines below.
3. **Run all test suites** and confirm they pass (see Testing below).
4. **Open a pull request** against `main` using the PR template.
5. Describe what changed, why, and how you tested it.

## Code Style

- **Python stdlib only.** No pip dependencies. This is a hard constraint. Every import must come from the Python standard library.
- **Follow existing patterns.** Read `broker.py` and `mcp_server.py` before adding code. Match the naming conventions, error handling style, and JSON response structure.
- **Type hints** are encouraged but not enforced.
- **Docstrings** for public functions and classes.
- **Keep it simple.** This project values clarity over abstraction. Prefer flat code over deep inheritance hierarchies.

## Testing

All five test suites must pass before submitting a PR:

```bash
python test_broker.py    # Unit tests for broker endpoints
python stress_test.py    # Concurrent load and edge cases
python e2e_test.py       # End-to-end workflow tests
python chaos_test.py     # Fault injection and recovery
python idiot_test.py     # Abuse scenarios and input validation
```

If you add a new feature or endpoint, add corresponding tests. If you fix a bug, add a regression test.

## Project Structure

| File | Purpose |
|---|---|
| `broker.py` | HTTP broker daemon (SQLite, auth, routing) |
| `mcp_server.py` | MCP server (stdio JSON-RPC, one per session) |
| `dashboard.html` | Standalone dashboard UI |
| `install.py` | Installer (adds MCP config to Claude Code) |
| `launch.py` | One-command launcher (broker + dashboard) |

## Commit Messages

- Use imperative mood: "Add task DAG validation" not "Added task DAG validation"
- Keep the first line under 72 characters
- Reference issue numbers where applicable: "Fix rate limiter reset (#42)"

## Security Issues

Report security vulnerabilities privately via the process described in [SECURITY.md](SECURITY.md). Do not file public issues for security bugs.
