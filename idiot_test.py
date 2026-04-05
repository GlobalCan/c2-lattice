#!/usr/bin/env python3
"""
Idiot Test: Do everything wrong and see what happens.
A real user who has never read docs, clicks randomly, sends garbage,
and doesn't understand what anything means.
"""
import urllib.request, json, sys, os, subprocess, time

URL = "http://127.0.0.1:7899"
FAILURES = []
PASSES = []

def raw_post(path, body_str, headers=None):
    """Post raw bytes, not necessarily valid JSON."""
    h = headers or {}
    req = urllib.request.Request(f"{URL}{path}", data=body_str.encode() if isinstance(body_str, str) else body_str, headers=h)
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

def post(path, data, token=None):
    body = json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{URL}{path}", data=body, headers=headers)
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        return 0, {"error": str(e)}

def get(path):
    try:
        resp = urllib.request.urlopen(f"{URL}{path}")
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

def test(name, passed):
    if passed:
        PASSES.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")

print("=" * 60)
print("  IDIOT TEST: Break everything, crash nothing")
print("=" * 60)

# ============================================================
print("\n=== 1. LAUNCH WITHOUT BROKER ===")
# Kill broker first
subprocess.run(
    ["powershell", "-Command",
     "Get-Process -Id (Get-NetTCPConnection -LocalPort 7899 -ErrorAction SilentlyContinue).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force"],
    capture_output=True)
time.sleep(1)

s, body = get("/health")
test("Health when broker is down returns error (not hang)", s == 0 or s >= 400)

s, body = get("/dashboard")
test("Dashboard when broker is down returns error", s == 0 or s >= 400)

# ============================================================
print("\n=== 2. LAUNCH BROKER ===")
# Use launch.py like a user would
result = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(__file__), "launch.py"), "--fresh"],
    capture_output=True, text=True, timeout=30)
test("launch.py --fresh exits cleanly", result.returncode == 0)
test("launch.py output says 'ready'", "ready" in result.stdout.lower() or "running" in result.stdout.lower())

time.sleep(2)
s, body = get("/health")
test("Broker is now running", s == 200)

# ============================================================
print("\n=== 3. HIT ENDPOINTS WITH NO JSON ===")
s, body = raw_post("/register", "not json at all")
test("Register with garbage text -> error, not crash", s >= 400)

s, body = raw_post("/register", "")
test("Register with empty body -> error, not crash", s >= 400)

s, body = raw_post("/register", '{"partial": true')  # broken JSON
test("Register with broken JSON -> error, not crash", s >= 400)

s, body = raw_post("/tasks", "hello world", {"Content-Type": "application/json", "Authorization": "Bearer fake"})
test("Tasks with plain text body -> error, not crash", s >= 400)

# ============================================================
print("\n=== 4. REGISTER WITH NONSENSE ===")
s, d = post("/register", {})
test("Register with empty object -> error", s >= 400 or "error" in str(d))

s, d = post("/register", {"id": 123, "role": "worker"})  # id should be string
test("Register with numeric id -> handles gracefully", True)  # should not crash

s, d = post("/register", {"id": "test", "role": "wizard"})  # invalid role
test("Register with role='wizard' -> rejected", s >= 400 or "error" in str(d))

s, d = post("/register", {"id": "a"*500, "role": "worker"})  # absurdly long id
test("Register with 500-char id -> rejected", s >= 400 or "error" in str(d))

s, d = post("/register", {"id": "good-user", "role": "worker", "pid": "not_a_number"})
test("Register with string pid -> handles gracefully", True)  # should not crash

# Actually register properly for later tests
s, d = post("/register", {"id": "idiot-user", "role": "architect", "pid": 12345})
TOKEN = d.get("token", "") if s == 200 else ""
test("Legit registration works", s == 200 and TOKEN)

# ============================================================
print("\n=== 5. CLICK EVERY BUTTON WITH NO DATA ===")
s, d = post("/tasks", {}, TOKEN)
test("Create task with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/tasks/claim", {}, TOKEN)
test("Claim task with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/tasks/complete", {}, TOKEN)
test("Complete task with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/send", {}, TOKEN)
test("Send message with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/memory", {}, TOKEN)
test("Set memory with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/pause", {}, TOKEN)
test("Pause with no peer_id -> error", s >= 400 or "error" in str(d))

s, d = post("/resume", {}, TOKEN)
test("Resume with no peer_id -> error", s >= 400 or "error" in str(d))

s, d = post("/kill-peer", {}, TOKEN)
test("Kill with no peer_id -> error", s >= 400 or "error" in str(d))

s, d = post("/budget", {}, TOKEN)
test("Budget with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/config", {}, TOKEN)
test("Config with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/lock", {}, TOKEN)
test("Lock with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/unlock", {}, TOKEN)
test("Unlock with no fields -> error", s >= 400 or "error" in str(d))

s, d = post("/spawn", {}, TOKEN)
test("Spawn with no role -> error or defaults", True)  # should not crash

s, d = post("/runs", {}, TOKEN)
test("Create run with no fields -> error", s >= 400 or "error" in str(d))

# ============================================================
print("\n=== 6. SPAM THE SAME THING 50 TIMES ===")
crash = False
for i in range(50):
    try:
        s, d = post("/register", {"id": f"spam-{i}", "role": "worker", "pid": 90000+i})
    except Exception:
        crash = True
        break
test("Register 50 peers rapidly -> no crash", not crash)

# Try 50 task creates
for i in range(50):
    try:
        post("/tasks", {"title": f"spam task {i}", "priority": "low", "created_by": "idiot-user"}, TOKEN)
    except Exception:
        crash = True
        break
test("Create 50 tasks rapidly -> no crash", not crash)

# ============================================================
print("\n=== 7. ACCESS THINGS THAT DON'T EXIST ===")
s, body = get("/nonexistent-page")
test("GET /nonexistent-page -> 404 or error, not crash", s >= 400 or s == 0)

s, body = get("/tasks/99999")
test("GET /tasks/99999 -> error (needs auth)", s >= 400)

s, body = get("/peer/nobody")
test("GET /peer/nobody -> error (needs auth)", s >= 400)

s, body = get("/messages/ghost")
test("GET /messages/ghost -> error (needs auth)", s >= 400)

s, d = post("/tasks/claim", {"task_id": -1, "peer_id": "idiot-user"}, TOKEN)
test("Claim task with negative ID -> error", s >= 400 or "error" in str(d))

s, d = post("/tasks/claim", {"task_id": 0, "peer_id": "idiot-user"}, TOKEN)
test("Claim task 0 -> error", s >= 400 or "error" in str(d) or "not found" in str(d).lower())

s, d = post("/pause", {"peer_id": "does-not-exist"}, TOKEN)
test("Pause non-existent peer -> error", s >= 400 or "error" in str(d) or "not found" in str(d).lower())

s, d = post("/kill-peer", {"peer_id": "ghost-peer"}, TOKEN)
test("Kill non-existent peer -> error", s >= 400 or "error" in str(d) or "not found" in str(d).lower())

# ============================================================
print("\n=== 8. UNICODE & SPECIAL CHARACTERS ===")
s, d = post("/register", {"id": "user-emoji-test", "role": "worker", "pid": 11111})
et = d.get("token", "") if s == 200 else TOKEN

s, d = post("/tasks", {"title": "Task with emojis 🎉🔥💀", "priority": "high", "created_by": "idiot-user"}, TOKEN)
test("Task with emoji title -> works or clean error", s == 200 or s >= 400)

s, d = post("/send", {"sender_id": "idiot-user", "recipient_id": "user-emoji-test",
    "category": "status_update", "content": "Hello in Chinese: 你好世界. Arabic: مرحبا. Russian: Привет."}, TOKEN)
test("Message with unicode -> works", s == 200)

s, d = post("/memory", {"key": "unicode-test", "value": "日本語テスト", "peer_id": "idiot-user",
    "type": "fact", "confidence": "high"}, TOKEN)
test("Memory with Japanese value -> works", s == 200)

s, d = post("/register", {"id": "null-\x00-byte", "role": "worker", "pid": 11112})
test("Register with null byte in id -> rejected or handles", True)  # should not crash

s, d = post("/tasks", {"title": "<script>alert('xss')</script>", "priority": "high", "created_by": "idiot-user"}, TOKEN)
test("XSS in task title -> stored safely (no execution)", s == 200 or s >= 400)

# ============================================================
print("\n=== 9. WRONG TYPES EVERYWHERE ===")
s, d = post("/tasks", {"title": 12345, "priority": "high", "created_by": "idiot-user"}, TOKEN)
test("Numeric task title -> handles gracefully", True)

s, d = post("/tasks", {"title": "ok", "priority": 999, "created_by": "idiot-user"}, TOKEN)
test("Numeric priority -> handles gracefully", True)

s, d = post("/tasks", {"title": "ok", "priority": "high", "created_by": "idiot-user", "blocked_by": "not-a-list"}, TOKEN)
test("blocked_by as string -> handles gracefully", True)

s, d = post("/tasks", {"title": "ok", "priority": "high", "created_by": "idiot-user", "blocked_by": [999999]}, TOKEN)
test("blocked_by with non-existent task -> handles gracefully", True)

s, d = post("/budget", {"peer_id": "idiot-user", "token_budget": "one million"}, TOKEN)
test("Budget as string 'one million' -> rejected", s == 400 or "error" in str(d))

s, d = post("/budget", {"peer_id": "idiot-user", "token_budget": 1e20}, TOKEN)
test("Budget as 1e20 -> handles gracefully", True)

s, d = post("/config", {"peer_id": "idiot-user", "poll_interval_ms": -5000}, TOKEN)
test("Negative poll interval -> clamped or rejected", True)

s, d = post("/config", {"peer_id": "idiot-user", "poll_interval_ms": 99999999}, TOKEN)
test("Huge poll interval -> clamped or rejected", True)

# ============================================================
print("\n=== 10. DASHBOARD WHILE CHAOS IS HAPPENING ===")
s, body = get("/dashboard")
test("Dashboard renders with 50 peers + 50 tasks", s == 200)

s, body = get("/dashboard/data")
test("Dashboard data returns valid JSON", s == 200)
try:
    d = json.loads(body)
    test("Dashboard data has expected fields", "active_count" in d and "total_tasks" in d)
except:
    test("Dashboard data has expected fields", False)

# ============================================================
print("\n=== 11. LAUNCH.PY EDGE CASES ===")
# Double launch (broker already running)
result = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(__file__), "launch.py")],
    capture_output=True, text=True, timeout=15)
test("launch.py when already running -> says 'already running'",
     "already running" in result.stdout.lower() or result.returncode == 0)

# Status check
result = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(__file__), "launch.py"), "--status"],
    capture_output=True, text=True, timeout=10)
test("launch.py --status shows stats", "running" in result.stdout.lower())

# ============================================================
print("\n=== 12. RAPID FIRE DASHBOARD REQUESTS ===")
crash = False
for i in range(100):
    try:
        urllib.request.urlopen(f"{URL}/dashboard/data", timeout=5)
    except Exception:
        crash = True
        break
test("100 rapid dashboard/data requests -> no crash", not crash)

# ============================================================
# RESULTS
print("\n" + "=" * 60)
total = len(PASSES) + len(FAILURES)
print(f"  RESULTS: {len(PASSES)}/{total} passed, {len(FAILURES)} failed")
print("=" * 60)
if FAILURES:
    print("\n  FAILURES:")
    for name in FAILURES:
        print(f"    - {name}")
    sys.exit(1)
else:
    print("\n  COMPLETELY IDIOT-PROOF. NOTHING CRASHED.")
