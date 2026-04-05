#!/usr/bin/env python3
"""
C2 Lattice — One-Command Installer

Usage:
    python install.py              Install and verify
    python install.py --uninstall  Remove everything
    python install.py --status     Check current status
"""

import json
import os
import subprocess
import sys
import socket
import time
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BROKER_PATH = os.path.join(SCRIPT_DIR, "broker.py")
MCP_SERVER_PATH = os.path.join(SCRIPT_DIR, "mcp_server.py")
DB_PATH = os.path.join(os.path.expanduser("~"), ".c2-lattice.db")
PORT = 7899
MCP_NAME = "c2-lattice"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}!{RESET} {msg}")


def info(msg):
    print(f"  {BLUE}→{RESET} {msg}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def is_broker_running() -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=1)
        sock.close()
        return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def is_mcp_registered() -> bool:
    """Check if c2-lattice is in ~/.claude.json mcpServers."""
    claude_json = os.path.join(os.path.expanduser("~"), ".claude.json")
    if not os.path.exists(claude_json):
        return False
    try:
        with open(claude_json, "r") as f:
            data = json.load(f)
        return MCP_NAME in data.get("mcpServers", {})
    except (json.JSONDecodeError, IOError):
        return False


def find_stale_mcp_json() -> list[str]:
    """Find .mcp.json files that might conflict."""
    stale = []
    home = os.path.expanduser("~")
    # Check home dir
    mcp_json = os.path.join(home, ".mcp.json")
    if os.path.exists(mcp_json):
        try:
            with open(mcp_json, "r") as f:
                data = json.load(f)
            if "c2-lattice" in data.get("mcpServers", {}):
                stale.append(mcp_json)
        except (json.JSONDecodeError, IOError):
            pass
    # Check project dir
    project_mcp = os.path.join(SCRIPT_DIR, ".mcp.json")
    if os.path.exists(project_mcp):
        stale.append(project_mcp)
    return stale


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install():
    print(f"\n{BOLD}C2 Lattice — Installer{RESET}\n")

    # Step 1: Check for stale .mcp.json
    stale = find_stale_mcp_json()
    if stale:
        for f in stale:
            warn(f"Found stale config: {f}")
            try:
                os.remove(f)
                ok(f"Removed {f}")
            except OSError as e:
                fail(f"Could not remove {f}: {e}")
                print(f"    Please delete it manually to avoid config conflicts.")
    else:
        ok("No stale .mcp.json files found")

    # Step 2: Register MCP server
    if is_mcp_registered():
        ok("MCP server already registered globally")
    else:
        info("Registering MCP server...")
        result = subprocess.run(
            [
                "claude", "mcp", "add",
                "--scope", "user",
                "--transport", "stdio",
                MCP_NAME,
                "--",
                sys.executable, MCP_SERVER_PATH.replace("\\", "/"),
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            ok("MCP server registered globally")
        else:
            fail(f"Registration failed: {result.stderr.strip()}")
            return False

    # Step 3: Start broker if not running
    if is_broker_running():
        ok("Broker already running on 127.0.0.1:7899")
    else:
        info("Starting broker...")
        try:
            if sys.platform == "win32":
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(
                    [sys.executable, BROKER_PATH],
                    creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [sys.executable, BROKER_PATH],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            # Wait for broker
            for _ in range(20):
                time.sleep(0.25)
                if is_broker_running():
                    break
            if is_broker_running():
                ok("Broker started on 127.0.0.1:7899")
            else:
                fail("Broker started but not responding after 5s")
                return False
        except Exception as e:
            fail(f"Could not start broker: {e}")
            return False

    # Step 4: Health check
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") == "ok":
            ok("Health check passed")
        else:
            fail(f"Health check returned unexpected: {data}")
            return False
    except Exception as e:
        fail(f"Health check failed: {e}")
        return False

    # Done
    print(f"\n{GREEN}{BOLD}Setup complete!{RESET}\n")
    print(f"  1. Open Claude Code:  {BOLD}claude{RESET}")
    print(f"  2. Type:              {BOLD}list all peers{RESET}")
    print(f"  3. Dashboard:         {BLUE}http://127.0.0.1:{PORT}/dashboard{RESET}")
    print(f"  4. Quickstart guide:  {BLUE}file:///{SCRIPT_DIR.replace(chr(92), '/')}/quickstart.html{RESET}")
    print()
    return True


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def uninstall():
    print(f"\n{BOLD}C2 Lattice — Uninstaller{RESET}\n")

    # Remove MCP registration
    if is_mcp_registered():
        result = subprocess.run(
            ["claude", "mcp", "remove", "--scope", "user", MCP_NAME],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            ok("MCP server unregistered")
        else:
            fail(f"Could not unregister: {result.stderr.strip()}")
    else:
        ok("MCP server not registered (nothing to remove)")

    # Kill broker
    if is_broker_running():
        info("Stopping broker...")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/shutdown",
                data=json.dumps({"requester": "installer"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        time.sleep(1)
        if not is_broker_running():
            ok("Broker stopped")
        else:
            warn("Broker still running — you may need to kill it manually")
    else:
        ok("Broker not running")

    # Remove DB
    for f in [DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    if not os.path.exists(DB_PATH):
        ok("Database removed")
    else:
        warn(f"Could not remove {DB_PATH} — may be locked by a running session")

    # Clean stale .mcp.json
    stale = find_stale_mcp_json()
    for f in stale:
        try:
            os.remove(f)
            ok(f"Removed stale {f}")
        except OSError:
            warn(f"Could not remove {f}")

    print(f"\n{GREEN}Uninstall complete.{RESET}\n")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status():
    print(f"\n{BOLD}C2 Lattice — Status{RESET}\n")

    # MCP
    if is_mcp_registered():
        ok("MCP server registered")
    else:
        fail("MCP server NOT registered")

    # Broker
    if is_broker_running():
        ok("Broker running on 127.0.0.1:7899")
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/peers")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            peers = data.get("peers", [])
            ok(f"{len(peers)} active peer(s)")
            for p in peers:
                info(f"  {p['id']} ({p['role']}) — {p.get('summary', '') or 'no summary'}")
        except Exception:
            warn("Could not fetch peer list")
    else:
        fail("Broker NOT running")

    # DB
    if os.path.exists(DB_PATH):
        size = os.path.getsize(DB_PATH)
        ok(f"Database exists ({size:,} bytes)")
    else:
        info("No database yet (created on first broker start)")

    # Stale configs
    stale = find_stale_mcp_json()
    if stale:
        for f in stale:
            warn(f"Stale config found: {f}")
    else:
        ok("No stale .mcp.json files")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    elif "--status" in sys.argv:
        status()
    else:
        install()
