#!/usr/bin/env python3
"""Full end-to-end pipeline test: Architect -> Workers -> Completion"""
import urllib.request, json, sys

URL = "http://127.0.0.1:7899"

def post(path, data, token=None):
    body = json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{URL}{path}", data=body, headers=headers)
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def get(path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{URL}{path}", headers=headers)
    return json.loads(urllib.request.urlopen(req).read())

print("=" * 50)
print("  FULL E2E TEST: Architect -> Workers Pipeline")
print("=" * 50)

# 1. Register
print("\n--- 1. Register Peers ---")
AT = post("/register", {"id": "lead-arch", "role": "architect", "pid": 70001})["token"]
W1T = post("/register", {"id": "worker-research", "role": "worker", "pid": 70002})["token"]
W2T = post("/register", {"id": "worker-builder", "role": "worker", "pid": 70003})["token"]
W3T = post("/register", {"id": "worker-outreach", "role": "worker", "pid": 70004})["token"]
print("  Registered: lead-arch, worker-research, worker-builder, worker-outreach")

# 2. Task DAG
print("\n--- 2. Create Task DAG ---")
t1 = post("/tasks", {"title": "Research Chatham dental market", "priority": "high", "created_by": "lead-arch"}, AT)["task_id"]
t2 = post("/tasks", {"title": "Build luxury landing page", "priority": "high", "created_by": "lead-arch", "blocked_by": [t1]}, AT)["task_id"]
t3 = post("/tasks", {"title": "Create outreach sequences", "priority": "medium", "created_by": "lead-arch", "blocked_by": [t1]}, AT)["task_id"]
t4 = post("/tasks", {"title": "Final review and delivery", "priority": "high", "created_by": "lead-arch", "blocked_by": [t2, t3]}, AT)["task_id"]
print(f"  #{t1}: Research (no deps)")
print(f"  #{t2}: Landing page (blocked by #{t1})")
print(f"  #{t3}: Outreach (blocked by #{t1})")
print(f"  #{t4}: Final review (blocked by #{t2},#{t3})")

# 3. Memory
print("\n--- 3. Set Shared Memory ---")
post("/memory", {"key": "target-market", "value": "High-income patients, Chatham-Kent, cosmetic dental", "peer_id": "lead-arch", "type": "decision", "confidence": "high"}, AT)
post("/memory", {"key": "brand-style", "value": "Luxury dark theme, gold accents", "peer_id": "lead-arch", "type": "decision", "confidence": "high"}, AT)
print("  Set: target-market, brand-style")

# 4. Assign
print("\n--- 4. Assign Work ---")
post("/send", {"sender_id": "lead-arch", "recipient_id": "worker-research", "category": "alert", "content": "Claim task #1. Research Chatham dental market."}, AT)
post("/send", {"sender_id": "lead-arch", "recipient_id": "worker-builder", "category": "alert", "content": "Wait for #1, then claim #2. Build landing page."}, AT)
post("/send", {"sender_id": "lead-arch", "recipient_id": "worker-outreach", "category": "alert", "content": "Wait for #1, then claim #3. Create outreach."}, AT)
print("  Sent 3 assignments")

# 5. Worker-research
print("\n--- 5. Worker-Research ---")
post("/tasks/claim", {"task_id": t1, "peer_id": "worker-research"}, W1T)
post("/summary", {"peer_id": "worker-research", "summary": "Researching Chatham dental market"}, W1T)
r = post("/tasks/complete", {"task_id": t1, "peer_id": "worker-research", "artifacts": {"summary": "15 practices found. Gaps: no Invisalign, no luxury cosmetic, no sedation.", "files_changed": ["research/market.md"]}}, W1T)
print(f"  Completed #{t1}. Unblocked: {[t['title'] for t in r.get('newly_unblocked', [])]}")

# 6. Worker-builder
print("\n--- 6. Worker-Builder ---")
post("/tasks/claim", {"task_id": t2, "peer_id": "worker-builder"}, W2T)
post("/summary", {"peer_id": "worker-builder", "summary": "Building luxury landing page"}, W2T)
post("/send", {"sender_id": "worker-builder", "recipient_id": "lead-arch", "category": "finding", "content": "Added before/after gallery. Dark theme with gold."}, W2T)
r = post("/tasks/complete", {"task_id": t2, "peer_id": "worker-builder", "artifacts": {"summary": "Landing page: hero, services, gallery, testimonials, contact. Mobile responsive.", "files_changed": ["site/index.html"]}}, W2T)
print(f"  Completed #{t2}. Unblocked: {[t['title'] for t in r.get('newly_unblocked', [])]}")

# 7. Worker-outreach (blocker flow)
print("\n--- 7. Worker-Outreach (blocker + resolve) ---")
post("/tasks/claim", {"task_id": t3, "peer_id": "worker-outreach"}, W3T)
post("/send", {"sender_id": "worker-outreach", "recipient_id": "lead-arch", "category": "blocker", "content": "Need practice name for templates."}, W3T)
print("  Raised blocker")
post("/send", {"sender_id": "lead-arch", "recipient_id": "worker-outreach", "category": "alert", "content": "Practice: Chatham Dental Excellence. Doctor: Dr. Sarah Chen."}, AT)
print("  Architect resolved")
r = post("/tasks/complete", {"task_id": t3, "peer_id": "worker-outreach", "artifacts": {"summary": "3 email sequences, 5 LinkedIn templates, 2 phone scripts.", "files_changed": ["outreach/emails.md"]}}, W3T)
print(f"  Completed #{t3}. Unblocked: {[t['title'] for t in r.get('newly_unblocked', [])]}")

# 8. Final review
print("\n--- 8. Final Review ---")
post("/tasks/claim", {"task_id": t4, "peer_id": "lead-arch"}, AT)
post("/tasks/complete", {"task_id": t4, "peer_id": "lead-arch", "artifacts": {"summary": "All deliverables reviewed. Package ready for client."}}, AT)
print(f"  Completed #{t4} - ALL TASKS DONE")

# 9. Report
post("/send", {"sender_id": "lead-arch", "recipient_id": "command-center", "category": "status_update", "content": "ALL COMPLETE. Research, landing page, outreach delivered. 3 workers, 4 tasks, 1 blocker resolved."}, AT)

# 10. Verify
print("\n--- FINAL STATE ---")
d = get("/dashboard/data")
tasks = get("/tasks", AT).get("tasks", [])
completed = sum(1 for t in tasks if t["status"] == "completed")
print(f"  Peers:    {d['active_count']} active")
print(f"  Tasks:    {completed}/{d['total_tasks']} completed")
print(f"  Messages: {d['total_messages']} ({d['unread_messages']} unread)")
print(f"  Escalations: {d.get('escalation_count', 0)}")
print()
for p in d.get("peers", []):
    print(f"  [{p.get('role','?'):9}] {p['id']}: {p.get('summary','')[:40]}")
print()
for t in tasks:
    icon = {"completed": "DONE", "in_progress": "WIP", "pending": "WAIT"}.get(t["status"], t["status"])
    print(f"  #{t['id']} [{icon:4}] {t['title']} -> {t.get('assigned_to','?')}")

# Assertions
ok = True
if completed != 4:
    print(f"\nFAIL: Expected 4 completed tasks, got {completed}")
    ok = False
if d["active_count"] != 4:
    print(f"\nFAIL: Expected 4 active peers, got {d['active_count']}")
    ok = False
if d["total_messages"] < 7:
    print(f"\nFAIL: Expected 7+ messages, got {d['total_messages']}")
    ok = False

print("\n" + "=" * 50)
if ok:
    print("  ALL ASSERTIONS PASSED")
else:
    print("  SOME ASSERTIONS FAILED")
    sys.exit(1)
print("=" * 50)
