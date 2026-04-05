#!/usr/bin/env python3
"""
Area10 Command Center — One-Command Launcher

Usage:
    python launch.py              Start broker + open dashboard
    python launch.py --fresh      Wipe DB first, clean slate
    python launch.py --status     Check if broker is running
    python launch.py --stop       Stop the broker
"""
import os
import sys
import time
import subprocess
import webbrowser
import urllib.request
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BROKER_PATH = os.path.join(SCRIPT_DIR, "broker.py")
PORT = int(os.environ.get("C2_LATTICE_PORT", "7899"))
URL = f"http://127.0.0.1:{PORT}"
DB_PATH = os.environ.get(
    "C2_LATTICE_DB",
    os.path.join(os.path.expanduser("~"), ".c2-lattice.db"),
)


def is_running():
    """Check if the broker is responding."""
    try:
        req = urllib.request.Request(f"{URL}/health")
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        return data.get("status") == "ok"
    except Exception:
        return False


def start_broker(fresh=False):
    """Start the broker daemon."""
    if is_running():
        print(f"Broker already running on port {PORT}")
        return True

    if fresh and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("Database wiped (fresh start)")

    print(f"Starting broker on port {PORT}...")
    if sys.platform == "win32":
        subprocess.Popen(
            [sys.executable, BROKER_PATH],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [sys.executable, BROKER_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for it to come up
    for i in range(20):
        time.sleep(0.5)
        if is_running():
            print(f"Broker started (took {(i+1)*0.5:.1f}s)")
            return True
    print("ERROR: Broker failed to start within 10 seconds")
    return False


def stop_broker():
    """Stop the broker."""
    if not is_running():
        print("Broker is not running")
        return
    # Register to get a token, then call shutdown
    try:
        data = json.dumps({"id": "launcher", "role": "architect", "pid": os.getpid()}).encode()
        req = urllib.request.Request(f"{URL}/register", data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        token = json.loads(resp.read()).get("token", "")
        if token:
            req2 = urllib.request.Request(
                f"{URL}/shutdown",
                data=json.dumps({"_": 1}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            )
            urllib.request.urlopen(req2)
    except Exception:
        pass

    # Force kill if still running
    time.sleep(1)
    if is_running():
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-Command",
                 f"Get-Process -Id (Get-NetTCPConnection -LocalPort {PORT} -ErrorAction SilentlyContinue).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force"],
                capture_output=True,
            )
        else:
            subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    print("Broker stopped")


def show_status():
    """Show broker status."""
    if is_running():
        try:
            resp = urllib.request.urlopen(f"{URL}/dashboard/data", timeout=2)
            d = json.loads(resp.read())
            print(f"Broker: RUNNING on port {PORT}")
            print(f"  Peers:    {d.get('active_count', 0)} active")
            print(f"  Tasks:    {d.get('total_tasks', 0)} ({d.get('tasks_completed', 0)} done)")
            print(f"  Messages: {d.get('total_messages', 0)} ({d.get('unread_messages', 0)} unread)")
            print(f"  Dashboard: {URL}/dashboard")
        except Exception:
            print(f"Broker: RUNNING on port {PORT}")
    else:
        print(f"Broker: NOT RUNNING")
        print(f"  Run: python launch.py")


def main():
    args = sys.argv[1:]

    if "--stop" in args:
        stop_broker()
        return

    if "--status" in args:
        show_status()
        return

    fresh = "--fresh" in args

    # Start broker
    if not start_broker(fresh=fresh):
        sys.exit(1)

    # Open dashboard in browser
    dashboard_url = f"{URL}/dashboard"
    print(f"Opening dashboard: {dashboard_url}")
    webbrowser.open(dashboard_url)

    print()
    print("=" * 50)
    print("  Area10 Command Center is ready!")
    print("=" * 50)
    print()
    print("  Dashboard:  " + dashboard_url)
    print("  Health:     " + f"{URL}/health")
    print()
    print("  Quick start:")
    print("    1. Click '+ Architect' in the dashboard")
    print("    2. Give it your task in the terminal that opens")
    print("    3. Watch it create tasks and spawn workers")
    print()
    print("  Commands:")
    print("    python launch.py --status   Check status")
    print("    python launch.py --stop     Stop broker")
    print("    python launch.py --fresh    Wipe DB + restart")
    print()


if __name__ == "__main__":
    main()
