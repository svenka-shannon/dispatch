#!/usr/bin/env -S uv run --script
"""
Smoke tests for claude-assistant performance & reliability.

Tests real integrations: iMessage, Signal, Agent SDK.
Run manually: uv run tests/smoke/run_smoke_tests.py [--skip-api]

Tests marked [API] hit the Claude API and cost credits.
Use --skip-api to skip those tests.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

HOME = Path.home()
CHAT_DB = HOME / "Library/Messages/chat.db"
SEND_SMS = HOME / ".claude/skills/sms-assistant/scripts/send-sms"
SEND_SIGNAL = HOME / "code/signal/send-signal"
TEST_MSG_DIR = HOME / ".claude/test-messages"
SIGNAL_SOCKET = Path("/tmp/signal-cli.sock")
OWN_PHONE = "+15555550001"  # Admin phone (sends to self)
SIGNAL_ACCOUNT = "+15555550002"

results: list[dict] = []


def report(name: str, passed: bool, duration: float, details: str = ""):
    status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  {status} {name} ({duration:.3f}s) {details}")
    results.append({"name": name, "passed": passed, "duration": duration, "details": details})


def section(title: str):
    print(f"\n{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 60}{RESET}")


# ──────────────────────────────────────────────────────────────
# Test 1: iMessage Send Latency
# ──────────────────────────────────────────────────────────────

def test_imessage_send_latency():
    section("Test 1: iMessage Send Latency")

    if not CHAT_DB.exists():
        report("chat.db exists", False, 0, "chat.db not found")
        return

    # Get current max ROWID
    db = sqlite3.connect(str(CHAT_DB))
    cur = db.execute("SELECT MAX(ROWID) FROM message")
    before_rowid = cur.fetchone()[0] or 0
    db.close()

    # Send test message
    test_text = f"SMOKE_TEST_{int(time.time())}"
    t0 = time.monotonic()
    result = subprocess.run(
        [str(SEND_SMS), OWN_PHONE, test_text],
        capture_output=True, text=True, timeout=30,
    )
    send_time = time.monotonic() - t0
    sent_ok = result.returncode == 0 and "SENT" in result.stdout
    report("send-sms executes", sent_ok, send_time, f"rc={result.returncode}")

    # Poll for message in chat.db
    t0 = time.monotonic()
    found = False
    is_from_me = None
    for _ in range(100):  # up to 10s
        time.sleep(0.1)
        db = sqlite3.connect(str(CHAT_DB))
        cur = db.execute(
            "SELECT ROWID, is_from_me FROM message WHERE ROWID > ? ORDER BY ROWID ASC",
            (before_rowid,),
        )
        rows = cur.fetchall()
        db.close()
        if rows:
            is_from_me = rows[0][1]
            found = True
            break

    db_time = time.monotonic() - t0
    report("message in chat.db", found, db_time)
    report("is_from_me=1 (loop safe)", is_from_me == 1, 0, f"is_from_me={is_from_me}")


# ──────────────────────────────────────────────────────────────
# Test 2: Signal Send Latency
# ──────────────────────────────────────────────────────────────

def test_signal_send_latency():
    section("Test 2: Signal Send Latency")

    if not SEND_SIGNAL.exists():
        report("send-signal exists", False, 0, "CLI not found")
        return

    # Send test message
    test_text = f"SIGNAL_SMOKE_{int(time.time())}"
    t0 = time.monotonic()
    result = subprocess.run(
        [str(SEND_SIGNAL), SIGNAL_ACCOUNT, test_text],
        capture_output=True, text=True, timeout=30,
    )
    send_time = time.monotonic() - t0
    sent_ok = result.returncode == 0 and "SUCCESS" in result.stdout
    report("send-signal executes", sent_ok, send_time, f"rc={result.returncode}")

    # Socket subscribe test
    if SIGNAL_SOCKET.exists():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            t0 = time.monotonic()
            sock.connect(str(SIGNAL_SOCKET))
            connect_time = time.monotonic() - t0

            subscribe = json.dumps({
                "jsonrpc": "2.0",
                "method": "subscribeReceive",
                "id": 1,
                "params": {},
            }) + "\n"
            sock.settimeout(3)
            sock.sendall(subscribe.encode())
            data = sock.recv(4096)
            subscribe_time = time.monotonic() - t0
            response = json.loads(data.decode())
            report("socket subscribe", "result" in response, subscribe_time)
            sock.close()
        except Exception as e:
            report("socket subscribe", False, 0, str(e))
    else:
        report("signal socket exists", False, 0, "socket not found")


# ──────────────────────────────────────────────────────────────
# Test 3: Daemon Message Throughput
# ──────────────────────────────────────────────────────────────

def test_daemon_throughput():
    section("Test 3: Daemon Message Throughput")

    TEST_MSG_DIR.mkdir(parents=True, exist_ok=True)

    # Check daemon is running
    status = subprocess.run(
        ["claude-assistant", "status"],
        capture_output=True, text=True, timeout=5,
    )
    daemon_running = status.returncode == 0 and "running" in status.stdout.lower()
    report("daemon running", daemon_running, 0)
    if not daemon_running:
        print(f"    {YELLOW}Skipping throughput test - daemon not running{RESET}")
        return

    # Burst 20 test messages (unknown contact to avoid session creation)
    msg_count = 20
    t0 = time.monotonic()
    paths = []
    for i in range(msg_count):
        msg = {
            "from": "+15555550000",
            "text": f"THROUGHPUT_{i}_{int(time.time())}",
            "is_group": False,
            "chat_id": "+15555550000",
        }
        path = TEST_MSG_DIR / f"smoke_throughput_{i:03d}.json"
        with open(path, "w") as f:
            json.dump(msg, f)
        paths.append(path)
    write_time = time.monotonic() - t0
    report(f"write {msg_count} test messages", True, write_time)

    # Wait for pickup (files deleted)
    t0 = time.monotonic()
    all_picked_up = False
    for attempt in range(100):  # up to 10s
        time.sleep(0.1)
        remaining = [p for p in paths if p.exists()]
        if not remaining:
            all_picked_up = True
            break

    pickup_time = time.monotonic() - t0
    remaining_count = len([p for p in paths if p.exists()])
    report(
        f"all {msg_count} messages picked up",
        all_picked_up,
        pickup_time,
        f"{remaining_count} remaining" if not all_picked_up else "",
    )

    # Cleanup any stragglers
    for p in paths:
        if p.exists():
            p.unlink()

    # Test with known contact (verify routing works)
    routing_msg = {
        "from": OWN_PHONE,
        "text": f"ROUTING_SMOKE_{int(time.time())}",
        "is_group": False,
        "chat_id": OWN_PHONE,
    }
    path = TEST_MSG_DIR / "smoke_routing.json"
    t0 = time.monotonic()
    with open(path, "w") as f:
        json.dump(routing_msg, f)

    for _ in range(50):
        time.sleep(0.1)
        if not path.exists():
            break
    routing_time = time.monotonic() - t0
    routed = not path.exists()
    report("known contact routed", routed, routing_time)
    if path.exists():
        path.unlink()


# ──────────────────────────────────────────────────────────────
# Test 4: Agent SDK Lifecycle [API]
# ──────────────────────────────────────────────────────────────

async def test_sdk_lifecycle():
    section("Test 4: Agent SDK Lifecycle [API]")

    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    opts = ClaudeAgentOptions(
        cwd="/tmp",
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        model="opus",
        max_turns=3,
    )

    # Connect
    client = ClaudeSDKClient(options=opts)
    t0 = time.monotonic()
    await client.connect()
    connect_time = time.monotonic() - t0
    report("SDK connect", True, connect_time)

    # Query
    t0 = time.monotonic()
    await client.query("Respond with exactly: PONG")
    first_msg_time = None
    session_id = None
    msg_count = 0
    async for message in client.receive_response():
        if first_msg_time is None:
            first_msg_time = time.monotonic() - t0
        msg_count += 1
        if hasattr(message, "session_id") and message.session_id:
            session_id = message.session_id
    query_time = time.monotonic() - t0
    report("SDK query + response", msg_count > 0, query_time, f"{msg_count} msgs, first@{first_msg_time:.3f}s")

    # Disconnect
    t0 = time.monotonic()
    await client.disconnect()
    disconnect_time = time.monotonic() - t0
    report("SDK disconnect", True, disconnect_time)

    # Resume
    if session_id:
        opts2 = ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=["Read"],
            permission_mode="bypassPermissions",
            model="opus",
            max_turns=3,
            resume=session_id,
        )
        client2 = ClaudeSDKClient(options=opts2)
        t0 = time.monotonic()
        await client2.connect()
        resume_connect = time.monotonic() - t0
        report("SDK resume connect", True, resume_connect)

        t0 = time.monotonic()
        await client2.query("What was my last message to you?")
        response_text = ""
        async for message in client2.receive_response():
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        response_text += block.text
        resume_time = time.monotonic() - t0
        recalls_context = "PONG" in response_text or "pong" in response_text.lower()
        report("SDK resume recalls context", recalls_context, resume_time, response_text[:80])
        await client2.disconnect()

    # Concurrent sessions
    t0 = time.monotonic()
    clients = []
    for _ in range(3):
        c = ClaudeSDKClient(options=ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=["Read"],
            permission_mode="bypassPermissions",
            model="opus",
            max_turns=2,
        ))
        clients.append(c)

    async def connect_and_disconnect(c):
        await c.connect()
        await c.disconnect()

    await asyncio.gather(*(connect_and_disconnect(c) for c in clients))
    concurrent_connect = time.monotonic() - t0
    all_connected = len(clients) == 3
    report("3 concurrent connects", all_connected, concurrent_connect)


# ──────────────────────────────────────────────────────────────
# Test 5: Interrupt & Failure Recovery [API]
# ──────────────────────────────────────────────────────────────

async def test_failure_recovery():
    section("Test 5: Interrupt & Failure Recovery [API]")

    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    opts = ClaudeAgentOptions(
        cwd="/tmp",
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        model="opus",
        max_turns=3,
    )

    client = ClaudeSDKClient(options=opts)
    await client.connect()

    # Interrupt mid-query
    await client.query("Write a 500-word essay about the history of computing")
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    await client.interrupt()
    interrupt_time = time.monotonic() - t0
    async for _ in client.receive_response():
        pass
    report("interrupt mid-query", True, interrupt_time)

    # Post-interrupt query
    t0 = time.monotonic()
    await client.query("Say OK")
    response = ""
    async for message in client.receive_response():
        if hasattr(message, "content"):
            for block in message.content:
                if hasattr(block, "text"):
                    response += block.text
    recovery_time = time.monotonic() - t0
    report("post-interrupt query works", len(response) > 0, recovery_time, response[:50])

    await client.disconnect()

    # Force disconnect during query
    client2 = ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd="/tmp",
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        model="opus",
        max_turns=3,
    ))
    await client2.connect()
    await client2.query("Write a long story about space exploration")
    await asyncio.sleep(0.3)

    t0 = time.monotonic()
    try:
        await client2.disconnect()
        disconnect_time = time.monotonic() - t0
        report("force disconnect during query", True, disconnect_time)
    except Exception as e:
        report("force disconnect during query", False, time.monotonic() - t0, str(e))


# ──────────────────────────────────────────────────────────────
# Test 6: Memory & Resource Leaks [API]
# ──────────────────────────────────────────────────────────────

async def test_memory_leaks():
    section("Test 6: Memory & Resource Leaks [API]")

    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    gc.collect()
    base_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    base_tasks = len(asyncio.all_tasks())

    # 5 create/destroy cycles
    t0 = time.monotonic()
    for i in range(5):
        client = ClaudeSDKClient(options=ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=["Read"],
            permission_mode="bypassPermissions",
            model="opus",
            max_turns=2,
        ))
        await client.connect()
        await client.query("Say hi")
        async for _ in client.receive_response():
            pass
        await client.disconnect()
        del client

    cycle_time = time.monotonic() - t0
    gc.collect()
    after_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    after_tasks = len(asyncio.all_tasks())

    mem_delta_kb = (after_mem - base_mem) // 1024
    task_delta = after_tasks - base_tasks
    report("5 create/destroy cycles", True, cycle_time, f"mem_delta={mem_delta_kb}KB")
    report("no task leaks", task_delta <= 1, 0, f"task_delta={task_delta}")
    report("memory delta <1MB", mem_delta_kb < 1024, 0, f"{mem_delta_kb}KB")


# ──────────────────────────────────────────────────────────────
# Test 7: Signal Socket Stress
# ──────────────────────────────────────────────────────────────

def test_signal_socket_stress():
    section("Test 7: Signal Socket Stress")

    if not SIGNAL_SOCKET.exists():
        report("signal socket exists", False, 0, "not found")
        return

    # Rapid connect/disconnect
    successes = 0
    failures = 0
    t0 = time.monotonic()
    for _ in range(10):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(SIGNAL_SOCKET))
            s.close()
            successes += 1
        except Exception:
            failures += 1
    stress_time = time.monotonic() - t0
    report(f"10x rapid connect/disconnect", failures == 0, stress_time, f"{successes}/10 ok")


# ──────────────────────────────────────────────────────────────
# Test 8: Registry Persistence Under Load
# ──────────────────────────────────────────────────────────────

def test_registry_persistence():
    section("Test 8: Registry Persistence Under Load")

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from assistant.sdk_backend import SessionRegistry

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        reg_file = Path(f.name)

    try:
        reg = SessionRegistry(reg_file)

        # Create 20 entries
        t0 = time.monotonic()
        for i in range(20):
            reg.register(
                chat_id=f"test:+1{i:010d}",
                session_name=f"smoke-user-{i}",
                tier="admin",
                contact_name=f"Smoke User {i}",
            )
        create_time = time.monotonic() - t0
        report("create 20 registry entries", True, create_time)

        # 2000 rapid updates
        t0 = time.monotonic()
        for _ in range(100):
            for i in range(20):
                reg.update_last_message_time(f"test:+1{i:010d}")
        update_time = time.monotonic() - t0
        report("2000 rapid updates", True, update_time)

        # Flush and verify
        reg.flush()
        file_size = reg_file.stat().st_size
        data = json.loads(reg_file.read_text())
        all_intact = all(
            data.get(f"test:+1{i:010d}", {}).get("contact_name") == f"Smoke User {i}"
            for i in range(20)
        )
        report("all data preserved", all_intact, 0, f"{len(data)} entries, {file_size} bytes")
        report("file size reasonable", file_size < 10000, 0, f"{file_size} bytes")

    finally:
        reg_file.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

async def run_api_tests():
    """Run tests that require the Claude API."""
    await test_sdk_lifecycle()
    await test_failure_recovery()
    await test_memory_leaks()


def main():
    skip_api = "--skip-api" in sys.argv

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Claude Assistant Smoke Tests{RESET}")
    print(f"{BOLD}  {'(skipping API tests)' if skip_api else '(including API tests - costs credits)'}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    t_start = time.monotonic()

    # Non-API tests
    test_imessage_send_latency()
    test_signal_send_latency()
    test_daemon_throughput()
    test_signal_socket_stress()
    test_registry_persistence()

    # API tests
    if not skip_api:
        asyncio.run(run_api_tests())
    else:
        section("Tests 4-6: Agent SDK [SKIPPED - use without --skip-api]")

    total_time = time.monotonic() - t_start

    # Summary
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    color = GREEN if failed == 0 else RED
    print(f"{BOLD}  {color}{passed} passed, {failed} failed{RESET} in {total_time:.1f}s")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    if failed > 0:
        print(f"{RED}Failed tests:{RESET}")
        for r in results:
            if not r["passed"]:
                print(f"  - {r['name']}: {r['details']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
