#!/usr/bin/env python3
"""CLI for managing the Claude Assistant daemon."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket as sock_module
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict

# Paths
ASSISTANT_DIR = Path(__file__).parent.parent
STATE_DIR = ASSISTANT_DIR / "state"
LOGS_DIR = ASSISTANT_DIR / "logs"
SESSION_LOG_DIR = LOGS_DIR / "sessions"
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = LOGS_DIR / "manager.log"

# Commands
import shutil
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")

# IPC socket
IPC_SOCKET = Path("/tmp/claude-assistant.sock")


def get_pid() -> Optional[int]:
    """Get the daemon PID if running."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process is actually running
        os.kill(pid, 0)
        # Verify it's actually our daemon (not a reused PID after reboot)
        import subprocess
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True
        )
        if "assistant" not in result.stdout:
            PID_FILE.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # PID file exists but process is dead
        PID_FILE.unlink(missing_ok=True)
        return None


def is_running() -> bool:
    """Check if daemon is running."""
    return get_pid() is not None


def _ipc_command(cmd: dict, timeout: float = 30) -> dict:
    """Send a command to the daemon via Unix socket IPC."""
    if not IPC_SOCKET.exists():
        print("Error: Daemon not running (IPC socket not found)", file=sys.stderr)
        print("Start with: claude-assistant start", file=sys.stderr)
        sys.exit(1)

    try:
        s = sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(IPC_SOCKET))
        s.sendall((json.dumps(cmd) + "\n").encode())

        # Read response
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        s.close()

        return json.loads(data.decode().strip())
    except ConnectionRefusedError:
        print("Error: Daemon not responding", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error communicating with daemon: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_start(args):
    """Start the daemon."""
    if is_running():
        print(f"Daemon already running (PID {get_pid()})")
        return 1

    # Ensure directories exist
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Start the manager as a background process
    log_fh = open(LOG_FILE, "a")
    process = subprocess.Popen(
        [UV, "run", "python", "-m", "assistant.manager"],
        cwd=ASSISTANT_DIR,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # Detach from terminal
    )
    log_fh.close()  # Popen has duped the fd; close ours (bug #27 fix)

    # Write PID file
    PID_FILE.write_text(str(process.pid))
    print(f"Daemon started (PID {process.pid})")
    print(f"Logs: {LOG_FILE}")
    return 0


def cmd_stop(args):
    """Stop the daemon."""
    pid = get_pid()
    if not pid:
        print("Daemon not running")
        return 1

    print(f"Stopping daemon (PID {pid})...")

    # Send SIGTERM to the entire process group (uv wrapper + child python process)
    # start_new_session=True in cmd_start creates a new process group with pgid=pid
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process already dead")
        PID_FILE.unlink(missing_ok=True)
        return 0
    except PermissionError:
        # Fallback to killing just the PID
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            print("Process already dead")
            PID_FILE.unlink(missing_ok=True)
            return 0

    # Wait for it to die
    for _ in range(10):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            break
    else:
        # Force kill the process group if still running
        print("Force killing...")
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    PID_FILE.unlink(missing_ok=True)
    print("Daemon stopped")
    return 0


def cmd_restart(args):
    """Restart the daemon via launchctl.

    Uses launchctl kickstart to get a clean environment from the plist,
    avoiding CLAUDECODE env var inheritance when called from SDK sessions.

    IMPORTANT: We must stop the daemon ourselves first because launchctl
    can only kill processes IT started. If the daemon was started via
    subprocess.Popen (e.g., from cli.py start), launchd doesn't own it
    and the -k flag does nothing.
    """
    import os
    from pathlib import Path

    # Write graceful restart marker so watchdog doesn't treat this as a crash
    # Include initiator_chat_id so sessions know who triggered the restart
    import json
    graceful_marker = Path("/tmp/dispatch-graceful-restart")
    marker_data: dict[str, int | str] = {"timestamp": int(time.time())}

    # Determine initiator: explicit --from flag, or auto-detect from cwd
    initiator = getattr(args, "initiator", None)
    if not initiator:
        # Auto-detect from cwd if running from a transcript directory
        # e.g. /Users/sven/transcripts/imessage/_15555550100 → +15555550100
        cwd = Path.cwd()
        transcripts_dir = Path.home() / "transcripts"
        try:
            rel = cwd.relative_to(transcripts_dir)
            parts = rel.parts  # e.g. ("imessage", "_15555550100")
            if len(parts) >= 2:
                sanitized_id = parts[1]  # e.g. "_15555550100"
                initiator = sanitized_id.replace("_", "+", 1)  # → "+15555550100"
        except ValueError:
            pass  # Not in transcripts dir

    if initiator:
        marker_data["initiator_chat_id"] = initiator
    graceful_marker.write_text(json.dumps(marker_data))

    # First, stop any existing daemon (launchctl -k can't kill what it didn't start)
    if is_running():
        print("Stopping existing daemon...")
        cmd_stop(args)
        # Wait a moment for cleanup
        time.sleep(0.5)

    # Now use launchctl to start fresh with clean environment
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "kickstart", f"gui/{uid}/com.dispatch.claude-assistant"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Daemon restarting via launchctl...")
    else:
        print(f"launchctl kickstart failed: {result.stderr}")
        return 1
    print("Restart initiated (detached)")
    return 0


def cmd_status(args):
    """Show daemon status."""
    pid = get_pid()
    if pid:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True
        )
        uptime = result.stdout.strip()
        print(f"Daemon running (PID {pid}, uptime {uptime})")

        # Show SDK auth mode (oauth vs api_key)
        from assistant import auth_mode
        am = auth_mode.current_mode()
        if am.get("mode") == "api_key":
            since = am.get("since", "?")[:19].replace("T", " ")
            reason = am.get("reason", "?")
            print(f"Auth: api_key (quota fallback since {since}, reason: {reason}). Run `claude-assistant auth reset` to flip back.")
        else:
            print("Auth: oauth (Max plan)")

        # Get sessions via IPC
        resp = _ipc_command({"cmd": "status"})
        if resp.get("ok") and resp.get("sessions"):
            print(f"\nActive sessions ({len(resp['sessions'])}):")
            for s in resp["sessions"]:
                busy = " [BUSY]" if s.get("is_busy") else ""
                turns = f" {s.get('turn_count', 0)} turns" if s.get('turn_count') else ""
                print(f"  {s.get('session_name', 'unknown'):20s} {s.get('contact_name', ''):20s} {s.get('tier', ''):10s}{busy}{turns}")
        else:
            print("\nNo active sessions")

        # Show memory consolidation status
        progress_file = Path.home() / "dispatch/state/consolidation-progress.json"
        consolidation_log = Path.home() / "dispatch/logs/memory-consolidation.log"
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text())
                if progress:
                    # Find most recent consolidation
                    latest_ts = None
                    for phone, data in progress.items():
                        ts = data.get("last_processed_ts", "")
                        if ts and (not latest_ts or ts > latest_ts):
                            latest_ts = ts
                    if latest_ts:
                        print(f"\nMemory consolidation: {len(progress)} contacts, last run {latest_ts[:16]}")
            except Exception:
                pass

        return 0
    else:
        print("Daemon not running")
        return 1


def cmd_logs(args):
    """Tail the log file."""
    if not LOG_FILE.exists():
        print(f"Log file not found: {LOG_FILE}")
        return 1

    lines = args.lines if hasattr(args, 'lines') else 50
    follow = args.follow if hasattr(args, 'follow') else False

    cmd = ["tail"]
    if follow:
        cmd.append("-f")
    cmd.extend(["-n", str(lines), str(LOG_FILE)])

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_attach(args):
    """Tail a session's log file."""
    session = args.session

    if not session:
        # List available session logs
        if SESSION_LOG_DIR.exists():
            logs = sorted(SESSION_LOG_DIR.glob("*.log"))
            if logs:
                print("Available session logs:")
                for log_file in logs:
                    name = log_file.stem
                    size = log_file.stat().st_size // 1024
                    print(f"  claude-assistant attach {name}  ({size}KB)")
            else:
                print("No session logs found")
        else:
            print("No session logs found")
        return 0

    log_file = SESSION_LOG_DIR / f"{session}.log"
    if not log_file.exists():
        print(f"No log file found for session: {session}")
        print(f"  Expected: {log_file}")
        return 1

    print(f"Tailing {log_file} (Ctrl+C to stop)")
    try:
        subprocess.run(["tail", "-f", "-n", "100", str(log_file)])
    except KeyboardInterrupt:
        pass
    return 0


def cmd_monitor(args):
    """Show live output from all session logs."""
    if not SESSION_LOG_DIR.exists():
        print("No session logs found")
        return 1

    logs = sorted(SESSION_LOG_DIR.glob("*.log"))
    if not logs:
        print("No session logs found")
        return 1

    print(f"Monitoring {len(logs)} session logs (Ctrl+C to stop)")
    try:
        subprocess.run(["tail", "-f"] + [str(l) for l in logs])
    except KeyboardInterrupt:
        pass
    return 0


def _load_registry() -> Dict:
    """Load the session registry."""
    from assistant.common import SESSION_REGISTRY_FILE
    if SESSION_REGISTRY_FILE.exists():
        try:
            return json.loads(SESSION_REGISTRY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _session_name_to_chat_id(session: str) -> Optional[str]:
    """Look up chat_id from registry by session name, chat_id, or contact name."""
    registry = _load_registry()
    # Try exact session_name match first
    for cid, data in registry.items():
        if data.get("session_name") == session:
            return cid
    # Try direct chat_id match
    if session in registry:
        return session
    # Try contact_name match (case-insensitive)
    session_lower = session.lower()
    for cid, data in registry.items():
        contact = data.get("contact_name", "")
        if contact.lower() == session_lower:
            return cid
    return None


def _session_not_found(session: str) -> int:
    """Print helpful error when session not found."""
    registry = _load_registry()
    print(f"Session not found: {session}")
    if registry:
        print("\nAvailable sessions:")
        for cid, data in registry.items():
            name = data.get("session_name", cid)
            contact = data.get("contact_name", "")
            print(f"  {name}  ({contact})" if contact else f"  {name}")
    return 1


def cmd_kill_session(args):
    """Kill a specific session."""
    session = args.session
    chat_id = _session_name_to_chat_id(session)

    if not chat_id:
        return _session_not_found(session)

    resp = _ipc_command({"cmd": "kill_session", "chat_id": chat_id})
    if resp.get("ok"):
        print(f"Killed session: {session}")
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def cmd_kill_sessions(args):
    """Kill all sessions."""
    resp = _ipc_command({"cmd": "kill_all_sessions"})
    print(resp.get("message", "Done"))
    return 0


def cmd_compact_session(args):
    """Compact a session.

    Compaction is now handled natively by Claude Code. Sessions compact
    automatically when context fills up. Use restart-session --clean
    for a completely fresh start.
    """
    print("Compaction is now handled natively by Claude Code.")
    print("Sessions compact automatically when context fills up.")
    print("Use 'restart-session --clean <session>' for a fresh start.")
    return 0


def cmd_restart_session(args):
    """Restart a specific session.

    By default, the session resumes from its previous state (native compaction).
    Use --clean to force a completely fresh session (clears session index and resume ID).
    """
    session = args.session
    chat_id = _session_name_to_chat_id(session)

    if not chat_id:
        return _session_not_found(session)

    is_clean = getattr(args, 'clean', False)

    # Build restart command with optional tier override and clean flag
    restart_cmd: dict[str, str | bool] = {"cmd": "restart_session", "chat_id": chat_id}
    if getattr(args, 'tier', None):
        restart_cmd["tier"] = args.tier
    if is_clean:
        restart_cmd["clean"] = True

    mode = "clean (fresh session)" if is_clean else "normal (with resume)"
    print(f"Restarting session ({mode})...")

    resp = _ipc_command(restart_cmd)
    if resp.get("ok"):
        print(f"Restarted session: {session}")
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def cmd_restart_sessions(args):
    """Restart all sessions."""
    # Get all sessions, restart each
    resp = _ipc_command({"cmd": "status"})
    if not resp.get("ok") or not resp.get("sessions"):
        print("No sessions to restart")
        return 0

    count = 0
    for s in resp["sessions"]:
        chat_id = s.get("chat_id")
        if chat_id:
            r = _ipc_command({"cmd": "restart_session", "chat_id": chat_id})
            name = s.get("session_name", chat_id)
            if r.get("ok"):
                print(f"Restarted: {name}")
                count += 1
            else:
                print(f"Failed to restart {name}: {r.get('error')}")

    print(f"\nRestarted {count} sessions")
    return 0


def cmd_restart_api(args):
    """Restart the dispatch API server without touching daemon or sessions."""
    print("Restarting dispatch API server...")
    resp = _ipc_command({"cmd": "restart_api"})
    if resp.get("ok"):
        print(resp.get("message", "Dispatch API restarted"))
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def cmd_set_global_model(args):
    """Set or clear the global model override for all sessions."""
    model = args.model
    resp = _ipc_command({"cmd": "set_global_model", "model": model})
    if resp.get("ok"):
        print(resp.get("message", "Done"))
        state = resp.get("state", "unknown")
        override = resp.get("override")
        print(f"Quota state: {state.upper()}")
        if override:
            print(f"Override: model={override.get('model')}, trigger={override.get('trigger')}, set_at={override.get('set_at')}")
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def cmd_get_global_model(args):
    """Show current global model override and quota state."""
    resp = _ipc_command({"cmd": "get_global_model"})
    if resp.get("ok"):
        state = resp.get("state", "unknown")
        override = resp.get("override")
        cb = resp.get("circuit_breaker", "unknown")
        q5h = resp.get("quota_5h_pct")
        q7d = resp.get("quota_7d_opus_pct")

        print(f"Quota state: {state.upper()}")
        if override:
            model = override.get("model", "?")
            trigger = override.get("trigger", "?")
            set_at = override.get("set_at", "?")
            print(f"Global model override: {model} (trigger={trigger}, set_at={set_at})")
        else:
            print("Global model override: none (using per-session defaults)")
        print(f"Deep heal circuit breaker: {cb.upper()}")

        if q5h is not None or q7d is not None:
            q5h_str = f"{q5h:.0f}%" if q5h is not None else "?"
            q7d_str = f"{q7d:.0f}%" if q7d is not None else "?"
            print(f"Quota: 5h={q5h_str} | 7d-opus={q7d_str}")

        # Dry-run: show what would happen
        if args.dry_run:
            from assistant.quota_manager import QuotaManager
            print()
            if q5h is not None and q7d is not None:
                if state == "normal":
                    if q5h >= QuotaManager.DEGRADE_THRESHOLD or q7d >= QuotaManager.DEGRADE_THRESHOLD:
                        print(f"[DRY-RUN] Would transition: NORMAL → DEGRADED (5h={q5h:.0f}% >= {QuotaManager.DEGRADE_THRESHOLD}%)")
                    else:
                        print(f"[DRY-RUN] No transition needed (5h={q5h:.0f}% < {QuotaManager.DEGRADE_THRESHOLD}%)")
                elif state == "degraded":
                    if q5h < QuotaManager.RECOVER_THRESHOLD and q7d < QuotaManager.RECOVER_THRESHOLD:
                        print(f"[DRY-RUN] Would transition: DEGRADED → NORMAL (5h={q5h:.0f}% < {QuotaManager.RECOVER_THRESHOLD}%)")
                    else:
                        print(f"[DRY-RUN] Would stay DEGRADED (5h={q5h:.0f}% >= {QuotaManager.RECOVER_THRESHOLD}%)")
            else:
                print("[DRY-RUN] Cannot evaluate — quota data unavailable")
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def cmd_set_model(args):
    """Set the model for a specific session."""
    session = args.session
    model = args.model

    # Validate model
    valid_models = ["opus", "sonnet", "haiku"]
    if model not in valid_models:
        print(f"Error: Invalid model '{model}'. Must be one of: {', '.join(valid_models)}")
        return 1

    chat_id = _session_name_to_chat_id(session)
    if not chat_id:
        return _session_not_found(session)

    resp = _ipc_command({"cmd": "set_model", "chat_id": chat_id, "model": model})
    if resp.get("ok"):
        print(f"Set model to '{model}' for session: {session}")
        print(f"Session restarted to apply new model.")
    else:
        print(f"Error: {resp.get('error', 'unknown')}")
    return 0 if resp.get("ok") else 1


def _lookup_contact_tier(contact_name: str) -> Optional[str]:
    """Look up a contact's tier from Contacts.app groups."""
    tier_groups = {
        "admin": "Claude Admin",
        "partner": "Claude Partner",
        "family": "Claude Family",
        "favorite": "Claude Favorites",
    }

    for tier, group_name in tier_groups.items():
        script = f'''
        tell application "Contacts"
            try
                set theGroup to group "{group_name}"
                set thePeople to people of theGroup
                repeat with p in thePeople
                    if name of p is "{contact_name}" then
                        return "{tier}"
                    end if
                end repeat
            end try
        end tell
        return ""
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True
        )
        if result.stdout.strip() == tier:
            return tier

    return None


def _lookup_contact_phone(contact_name: str) -> Optional[str]:
    """Look up a contact's phone number from Contacts.app."""
    script = f'''
    tell application "Contacts"
        try
            set thePerson to first person whose name is "{contact_name}"
            set thePhones to phones of thePerson
            if (count of thePhones) > 0 then
                return value of first item of thePhones
            end if
        end try
    end tell
    return ""
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    phone = result.stdout.strip()
    if phone:
        # Normalize: ensure it starts with +
        if not phone.startswith("+"):
            phone = phone.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
            if len(phone) == 10:
                phone = f"+1{phone}"
            elif len(phone) == 11 and phone.startswith("1"):
                phone = f"+{phone}"
        return phone
    return None


def _lookup_contact_by_phone(phone: str) -> Optional[Dict[str, str]]:
    """Lookup contact info by phone number from Contacts.app."""
    contacts_cli = Path.home() / ".claude/skills/contacts/scripts/contacts"
    if not contacts_cli.exists():
        return None

    result = subprocess.run(
        [str(contacts_cli), "lookup", phone],
        capture_output=True, text=True
    )
    output = result.stdout.strip()

    # Output format: "Name | +1234567890 | tier"
    if output and "|" in output:
        parts = [p.strip() for p in output.split("|")]
        if len(parts) >= 3:
            return {
                "name": parts[0],
                "phone": parts[1],
                "tier": parts[2]
            }
    return None


def cmd_inject_prompt(args):
    """Inject a prompt into a contact's Claude session."""
    from assistant.common import normalize_chat_id, is_group_chat_id

    # Accept session_name format (e.g. imessage/_15555550100) or raw chat_id
    raw = args.chat_id
    resolved = _session_name_to_chat_id(raw)
    chat_id = normalize_chat_id(resolved if resolved else raw)

    # Get prompt
    if args.file:
        try:
            prompt = Path(args.file).read_text()
        except Exception as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            return 1
    else:
        prompt = args.prompt

    if not prompt:
        print("Error: No prompt provided", file=sys.stderr)
        return 1

    # Look up contact info
    from assistant.common import SESSION_REGISTRY_FILE
    from assistant.sdk_backend import SessionRegistry
    registry = SessionRegistry(SESSION_REGISTRY_FILE)
    session_data = registry.get(chat_id)

    from assistant.backends import BACKENDS
    source = "imessage"
    for backend_name, cfg in BACKENDS.items():
        if cfg.registry_prefix and chat_id.startswith(cfg.registry_prefix):
            source = backend_name
            break

    if session_data:
        contact_name = session_data.get("contact_name") or session_data.get("display_name", "Unknown")
        tier = session_data.get("tier", "favorite")
    else:
        # Look up from Contacts
        lookup_phone = chat_id.removeprefix(BACKENDS[source].registry_prefix) if BACKENDS[source].registry_prefix else chat_id
        contact_info = _lookup_contact_by_phone(lookup_phone)
        if contact_info:
            contact_name = contact_info["name"]
            tier = contact_info["tier"]
        else:
            # Auto-create session for unknown contacts
            is_group = is_group_chat_id(chat_id)
            if is_group:
                contact_name = f"Group {chat_id[:8]}"
            else:
                contact_name = f"Unknown ({lookup_phone})"
            tier = "favorite"  # Safe default tier
            print(f"Auto-creating session for: {chat_id} (tier={tier})", file=sys.stderr)

    # --admin flag overrides tier to admin (fixes permission issues when
    # registry has wrong tier cached)
    if args.admin:
        tier = "admin"

    # Build attachment info if provided
    attachment = None
    if getattr(args, 'attachment', None):
        attachment_path = Path(args.attachment).expanduser()
        if attachment_path.exists():
            import mimetypes
            mime_type = mimetypes.guess_type(str(attachment_path))[0] or "application/octet-stream"
            attachment = {
                "path": str(attachment_path),
                "name": attachment_path.name,
                "mime_type": mime_type,
                "size": attachment_path.stat().st_size,
            }
        else:
            print(f"Warning: Attachment file not found: {attachment_path}", file=sys.stderr)

    resp = _ipc_command({
        "cmd": "inject",
        "chat_id": chat_id,
        "prompt": prompt,
        "sms": args.sms,
        "admin": args.admin,
        "app": getattr(args, 'app', False),
        "contact_name": contact_name,
        "tier": tier,
        "source": source,
        "reply_to": getattr(args, 'reply_to', None),
        "attachment": attachment,
    })

    if resp.get("ok"):
        print(resp.get("message", "Injected"))
    else:
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
    return 0 if resp.get("ok") else 1


def cmd_install(args):
    """Install LaunchAgent for auto-start on boot."""
    plist_src = ASSISTANT_DIR / "launchd" / "com.dispatch.claude-assistant.plist"
    plist_dst = Path.home() / "Library/LaunchAgents/com.dispatch.claude-assistant.plist"

    if not plist_src.exists():
        print(f"LaunchAgent plist not found: {plist_src}")
        return 1

    # Create LaunchAgents directory if needed
    plist_dst.parent.mkdir(parents=True, exist_ok=True)

    # Copy plist
    plist_dst.write_text(plist_src.read_text())
    print(f"Installed: {plist_dst}")

    # Load the agent
    subprocess.run(["launchctl", "load", str(plist_dst)])
    print("LaunchAgent loaded - daemon will start on login")
    return 0


def cmd_uninstall(args):
    """Uninstall LaunchAgent."""
    plist_dst = Path.home() / "Library/LaunchAgents/com.dispatch.claude-assistant.plist"

    if not plist_dst.exists():
        print("LaunchAgent not installed")
        return 1

    # Unload the agent
    subprocess.run(["launchctl", "unload", str(plist_dst)], capture_output=True)

    # Remove plist
    plist_dst.unlink()
    print("LaunchAgent uninstalled")
    return 0


# Menu bar app commands
MENUBAR_PLIST_SRC = ASSISTANT_DIR / "launchd" / "com.dispatch.claude-menubar.plist"
MENUBAR_PLIST_DST = Path.home() / "Library/LaunchAgents/com.dispatch.claude-menubar.plist"


# Watchdog commands
WATCHDOG_PLIST_SRC = ASSISTANT_DIR / "launchd" / "com.dispatch.watchdog.plist"
WATCHDOG_PLIST_DST = Path.home() / "Library/LaunchAgents/com.dispatch.watchdog.plist"


def cmd_watchdog_install(args):
  """Install watchdog LaunchAgent for auto-recovery."""
  if not WATCHDOG_PLIST_SRC.exists():
    print(f"Watchdog plist not found: {WATCHDOG_PLIST_SRC}")
    return 1

  # Create LaunchAgents directory if needed
  WATCHDOG_PLIST_DST.parent.mkdir(parents=True, exist_ok=True)

  # Copy plist
  WATCHDOG_PLIST_DST.write_text(WATCHDOG_PLIST_SRC.read_text())
  print(f"Installed: {WATCHDOG_PLIST_DST}")

  # Load the agent
  subprocess.run(["launchctl", "load", str(WATCHDOG_PLIST_DST)])
  print("Watchdog loaded - will check daemon health every 60s")
  return 0


def cmd_watchdog_uninstall(args):
  """Uninstall watchdog LaunchAgent."""
  if not WATCHDOG_PLIST_DST.exists():
    print("Watchdog not installed")
    return 1

  # Unload the agent
  subprocess.run(["launchctl", "unload", str(WATCHDOG_PLIST_DST)], capture_output=True)

  # Remove plist
  WATCHDOG_PLIST_DST.unlink()
  print("Watchdog uninstalled")
  return 0


def cmd_watchdog_status(args):
  """Show watchdog status."""
  if WATCHDOG_PLIST_DST.exists():
    result = subprocess.run(
      ["launchctl", "list", "com.dispatch.watchdog"],
      capture_output=True, text=True
    )
    if result.returncode == 0:
      print("Watchdog: installed and running")
      # Check crash state
      crash_state = Path("/tmp/dispatch-watchdog-crashes.txt")
      if crash_state.exists():
        try:
          crash_count, _ = crash_state.read_text().strip().split()
          print(f"  Recent crashes: {crash_count}")
        except Exception:
          pass
      # Show recent log
      log_file = Path.home() / "dispatch/logs/watchdog.log"
      if log_file.exists():
        lines = log_file.read_text().strip().split("\n")[-3:]
        if lines:
          print("  Recent log:")
          for line in lines:
            print(f"    {line}")
    else:
      print("Watchdog: installed but not running")
  else:
    print("Watchdog: not installed")
    print("  Install with: claude-assistant watchdog-install")
  return 0


def cmd_auth(args):
    """Manage SDK auth mode (oauth vs api_key fallback)."""
    from assistant import auth_mode

    sub = getattr(args, "auth_command", None)
    if sub == "status" or sub is None:
        info = auth_mode.current_mode()
        mode = info.get("mode", "oauth")
        if mode == "api_key":
            since = info.get("since", "?")[:19].replace("T", " ")
            print(f"mode: api_key")
            print(f"since: {since}")
            print(f"reason: {info.get('reason', '?')}")
            if info.get("triggered_by_session"):
                print(f"triggered_by_session: {info['triggered_by_session']}")
            if info.get("error_text"):
                print(f"error_text: {info['error_text']}")
            print("\nTo flip back to OAuth: claude-assistant auth reset && claude-assistant restart")
        else:
            print("mode: oauth (Max plan quota)")
            fb = "set" if os.environ.get("ANTHROPIC_API_KEY_FALLBACK") else "NOT SET"
            print(f"ANTHROPIC_API_KEY_FALLBACK: {fb}")
        return 0

    if sub == "reset":
        removed = auth_mode.clear()
        if removed:
            print("auth_mode.json cleared. Next daemon restart will use OAuth.")
            print("Run: claude-assistant restart")
        else:
            print("auth_mode.json was not present — already on OAuth.")
        return 0

    print(f"Unknown auth subcommand: {sub}")
    return 1


def cmd_remind(args):
    """Manage native reminders."""
    from assistant.reminders import (
        add_reminder_cli, list_reminders_cli, cancel_reminder_cli,
        retry_reminder_cli, preview_cron_cli, format_for_display
    )

    if not args.remind_command or args.remind_command == "add":
        # Handle add command
        if not hasattr(args, 'title') or not args.title:
            print("Usage: claude-assistant remind add 'title' --contact NAME --in 2h")
            print("       claude-assistant remind add 'title' --event '{...}' --cron '0 2 * * *'")
            return 1

        event_json = getattr(args, 'event_json', None)
        if not args.contact and not event_json:
            print("Error: Must specify --contact or --event")
            return 1

        if not args.in_duration and not args.at_time and not args.cron:
            print("Error: Must specify --in, --at, or --cron")
            return 1

        try:
            reminder = add_reminder_cli(
                title=args.title,
                contact=args.contact,
                in_duration=args.in_duration,
                at_time=args.at_time,
                cron_pattern=args.cron,
                tz_override=args.tz,
                target=args.target,
                event_json=event_json
            )
            tz = args.tz or "America/New_York"
            display_time = format_for_display(reminder["next_fire"], tz)
            print(f"Created: {reminder['id']} → {reminder['title']}")
            print(f"  Fires: {display_time}")
            if reminder.get('contact'):
                print(f"  Contact: {reminder['contact']}")
                print(f"  Target: {reminder['target']}")
            elif reminder.get('event'):
                print(f"  Event: {reminder['event']['type']}")
            return 0
        except Exception as e:
            print(f"Error: {e}")
            return 1

    elif args.remind_command == "list":
        try:
            reminders = list_reminders_cli(
                contact=args.contact,
                show_failed=args.failed
            )
            if not reminders:
                print("No reminders found.")
                return 0

            # Print table
            print(f"{'ID':<10} {'Title':<30} {'Next Fire':<25} {'Contact/Event':<15}")
            print("-" * 80)
            for r in reminders:
                title = r['title'][:28] + '..' if len(r['title']) > 30 else r['title']
                display = r.get('_display_time', r['next_fire'])[:23]
                # Handle both legacy (contact) and generalized (event) reminders
                if r.get('contact'):
                    contact_col = r['contact']
                elif r.get('event'):
                    contact_col = f"Event: {r['event'].get('type', '?')}"
                else:
                    contact_col = "(none)"
                contact_col = contact_col[:13] + '..' if len(contact_col) > 15 else contact_col
                status = ""
                if r.get('retry_count', 0) >= 3:
                    status = " [DEAD]"
                elif r.get('last_error'):
                    status = f" [retry {r['retry_count']}]"
                print(f"{r['id']:<10} {title:<30} {display:<25} {contact_col:<15}{status}")
            return 0
        except Exception as e:
            print(f"Error: {e}")
            return 1

    elif args.remind_command == "cancel":
        if not args.id and not args.title:
            print("Error: Must specify reminder ID or --title")
            return 1

        try:
            count = cancel_reminder_cli(
                reminder_id=args.id,
                title=args.title,
                force=args.force
            )
            if count > 0:
                print(f"Cancelled {count} reminder(s)")
                return 0
            else:
                print("No matching reminders found")
                return 1
        except ValueError as e:
            print(f"Error: {e}")
            return 1

    elif args.remind_command == "retry":
        if not args.id:
            print("Error: Must specify reminder ID")
            return 1

        if retry_reminder_cli(args.id):
            print(f"Reset reminder {args.id} - will retry on next poll")
            return 0
        else:
            print(f"Reminder {args.id} not found")
            return 1

    elif args.remind_command == "next":
        try:
            times = preview_cron_cli(args.pattern, args.tz, args.n)
            print(f"Next {args.n} fire times for '{args.pattern}':")
            for t in times:
                print(f"  {t}")
            return 0
        except Exception as e:
            print(f"Error: {e}")
            return 1

    else:
        print(f"Unknown remind command: {args.remind_command}")
        return 1


def _session_name_from_cwd() -> Optional[str]:
    """Derive the session name ({backend}/{folder}) from the caller's working dir.

    Sessions run with cwd = ~/transcripts/{backend}/{sanitized_chat_id}/, which
    is exactly the session name. The `claude-assistant` wrapper exports
    DISPATCH_CALLER_CWD before it `cd`s into the repo, so prefer that; fall back
    to the process cwd. Returns None if not inside a transcript dir.
    """
    transcripts_dir = (Path.home() / "transcripts").resolve()
    candidate = os.environ.get("DISPATCH_CALLER_CWD") or os.getcwd()
    try:
        rel = Path(candidate).resolve().relative_to(transcripts_dir)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def cmd_commitment(args):
    """Set/clear/show a session's outstanding-commitment marker.

    A session should `commitment set "<what>"` whenever it tells the user
    "I'll do X" / "waiting on you to do Y", and `commitment clear` once that's
    resolved (or escalated). The daemon's blocked-session watchdog nudges any
    idle session that still has a marker — so a stuck task never just stalls.
    """
    from assistant.commitments import set_commitment, clear_commitment, get_commitment

    session_name = getattr(args, "session", None) or _session_name_from_cwd()
    if not session_name:
        print("Error: could not determine session — run from a transcript dir or pass --session <backend>/<id>", file=sys.stderr)
        return 1

    sub = getattr(args, "commitment_command", None)
    if sub == "set":
        text = (args.text or "").strip()
        if not text:
            print("Error: commitment text is required", file=sys.stderr)
            return 1
        path = set_commitment(session_name, text)
        print(f"Commitment set for {session_name}: {text}\n  ({path})")
        return 0
    elif sub == "clear":
        existed = clear_commitment(session_name)
        print(f"Commitment cleared for {session_name}" if existed else f"No commitment was set for {session_name}")
        return 0
    elif sub == "show" or sub is None:
        c = get_commitment(session_name)
        if c:
            print(f"{session_name}: {c.get('text')}  (set at {c.get('set_at')})")
        else:
            print(f"{session_name}: no outstanding commitment")
        return 0
    else:
        print(f"Unknown commitment command: {sub}", file=sys.stderr)
        return 1


def cmd_health_history(args):
    """Show the resilience / health-history report (self-healing-resilience §4.4).

    Queries state/bus.db for health.dependency (and session.stuck_nudge,
    health.haiku_verdict/circuit_breaker/quota_alert) over a window and prints
    per-dependency current state, recovery counts, MTTR P50/P95, last escalation,
    the recovery-frequency alarm status, a transition tail, and a one-line SLO check.
    """
    from assistant import health_report

    db_path = getattr(args, "db", None) or (STATE_DIR / "bus.db")
    try:
        conn = health_report.open_bus_db(db_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    try:
        report = health_report.build_report(
            conn,
            hours=args.hours,
            dep=args.dep,
            transition_limit=args.limit,
        )
    finally:
        conn.close()

    if getattr(args, "json", False):
        # Emit a JSON view (no truncation).
        def _dep_json(s):
            return {
                "name": s.name,
                "current_state": s.current_state,
                "last_event": health_report._fmt_ts(s.last_event_ms),
                "recoveries_1h": s.recoveries_1h,
                "recoveries_24h": s.recoveries_24h,
                "recovery_attempts": s.recovery_attempts,
                "mttr_p50_s": s.mttr_p50_s,
                "mttr_p95_s": s.mttr_p95_s,
                "incident_count": s.incident_count,
                "ongoing_incident": (
                    {"start": health_report._fmt_ts(s.ongoing_incident.start_ms),
                     "escalated": s.ongoing_incident.escalated}
                    if s.ongoing_incident else None
                ),
                "last_escalation": health_report._fmt_ts(s.last_escalation_ms),
                "last_escalation_detail": s.last_escalation_detail,
                "recovery_alarm_fired": s.recovery_alarm_fired,
                "slo_misses": s.slo_misses,
            }
        out = {
            "window_hours": report.window_hours,
            "window_start": health_report._fmt_ts(report.window_start_ms),
            "now": health_report._fmt_ts(report.now_ms),
            "slo_ok": report.slo_ok,
            "slo_misses": report.all_slo_misses,
            "dependencies": {n: _dep_json(s) for n, s in report.dependencies.items()},
            "stuck_nudge_count": len(report.stuck_nudges),
            "transition_count": len(report.transitions),
        }
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(health_report.render_report(report, dep_filter=args.dep))
    # Exit 2 if any SLO miss (so it can gate scripts/CI), 0 otherwise.
    return 0 if report.slo_ok else 2


def cmd_menubar(args):
    """Start the menu bar app (foreground)."""
    menubar_script = ASSISTANT_DIR / "bin" / "claude-menubar"
    if not menubar_script.exists():
        print(f"Menu bar app not found: {menubar_script}")
        return 1
    os.execvp(str(menubar_script), [str(menubar_script)])


def cmd_menubar_install(args):
    """Install menu bar LaunchAgent for auto-start."""
    if not MENUBAR_PLIST_SRC.exists():
        print(f"Menu bar plist not found: {MENUBAR_PLIST_SRC}")
        return 1

    # Create LaunchAgents directory if needed
    MENUBAR_PLIST_DST.parent.mkdir(parents=True, exist_ok=True)

    # Copy plist
    MENUBAR_PLIST_DST.write_text(MENUBAR_PLIST_SRC.read_text())
    print(f"Installed: {MENUBAR_PLIST_DST}")

    # Load the agent
    subprocess.run(["launchctl", "load", str(MENUBAR_PLIST_DST)])
    print("Menu bar app will start on login and is now running")
    return 0


def cmd_menubar_uninstall(args):
    """Uninstall menu bar LaunchAgent."""
    if not MENUBAR_PLIST_DST.exists():
        print("Menu bar LaunchAgent not installed")
        return 1

    # Unload the agent
    subprocess.run(["launchctl", "unload", str(MENUBAR_PLIST_DST)], capture_output=True)

    # Remove plist
    MENUBAR_PLIST_DST.unlink()
    print("Menu bar LaunchAgent uninstalled")
    return 0


def _detect_session_from_cwd() -> str | None:
    """Resolve the session_name (or chat_id) of the current transcript dir.

    Mirrors the heuristic used by ~/.claude/skills/sms-assistant/scripts/reply:
    walks the registry for an entry whose transcript_dir matches cwd, returns
    the chat_id (which is what the daemon uses as the session key).
    """
    transcripts_dir = Path.home() / "transcripts"
    cwd = Path.cwd()
    try:
        parts = cwd.relative_to(transcripts_dir).parts
    except ValueError:
        return None
    if len(parts) < 2:
        return None

    expected = str(transcripts_dir / parts[0] / parts[1])
    registry_path = Path.home() / "dispatch/state/sessions.json"
    try:
        registry = json.loads(registry_path.read_text())
    except Exception:
        return None
    for entry in registry.values():
        if entry.get("transcript_dir") == expected:
            return entry.get("chat_id")
    return None


def cmd_dispatch_investigation(args):
    """Dispatch a background investigation from the current session."""
    from_session = args.from_session or _detect_session_from_cwd()
    if not from_session:
        print("Error: Could not detect originating session from cwd. "
              "Pass --from-session.", file=sys.stderr)
        return 1

    prompt = args.prompt
    if args.file:
        try:
            prompt = Path(args.file).read_text()
        except Exception as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            return 1
    if not prompt:
        print("Error: prompt required (positional arg or --file)", file=sys.stderr)
        return 1

    resp = _ipc_command({
        "cmd": "investigation_dispatch",
        "from_session": from_session,
        "prompt": prompt,
        "allow_mutations": args.allow_mutations,
        "timeout_minutes": args.timeout_minutes,
    })
    if resp.get("ok"):
        print(resp.get("message", f"Dispatched: {resp.get('task_id')}"))
        return 0
    print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
    return 1


def cmd_investigation_status(args):
    """Show status of a single investigation."""
    resp = _ipc_command({
        "cmd": "investigation_status",
        "task_id": args.task_id,
    })
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(json.dumps(resp.get("investigation"), indent=2, sort_keys=True))
    return 0


def cmd_investigation_list(args):
    """List in-flight (and recent) investigations."""
    payload: dict = {"cmd": "investigation_list"}
    if args.from_session:
        payload["from_session"] = args.from_session
    elif args.mine:
        detected = _detect_session_from_cwd()
        if detected:
            payload["from_session"] = detected

    resp = _ipc_command(payload)
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    investigations = resp.get("investigations", [])
    if not investigations:
        print("No investigations.")
        return 0
    for entry in investigations:
        print(f"{entry.get('task_id')}  {entry.get('status'):<10}  "
              f"from={entry.get('from_session')}  "
              f"created={entry.get('created_at')}")
        prompt = entry.get("prompt", "")
        if prompt:
            first = prompt.strip().splitlines()[0][:100]
            print(f"  {first}")
    return 0


def cmd_investigation_cancel(args):
    """Cancel a running investigation."""
    resp = _ipc_command({
        "cmd": "investigation_cancel",
        "task_id": args.task_id,
    })
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(resp.get("message", "Cancelled"))
    return 0


def main():
    # Load ~/.secrets.env (via ~/dispatch/.env) so commands like `auth status`
    # can see ANTHROPIC_API_KEY_FALLBACK without the daemon being involved.
    from assistant.common import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="claude-assistant",
        description="Manage the Claude Assistant daemon"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # start
    subparsers.add_parser("start", help="Start the daemon")

    # stop
    subparsers.add_parser("stop", help="Stop the daemon")

    # restart
    restart_parser = subparsers.add_parser("restart", help="Restart the daemon")
    restart_parser.add_argument("--from", dest="initiator", help="Chat ID of the session that initiated the restart")

    # status
    subparsers.add_parser("status", help="Show daemon status")

    # logs
    logs_parser = subparsers.add_parser("logs", help="Tail the log file")
    logs_parser.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output (tail -f)")

    # attach
    attach_parser = subparsers.add_parser("attach", help="Tail a session log file")
    attach_parser.add_argument("session", nargs="?", help="Session name")

    # monitor
    subparsers.add_parser("monitor", help="Show live output from all session logs")

    # kill-session
    kill_session_parser = subparsers.add_parser("kill-session", help="Kill a specific session")
    kill_session_parser.add_argument("session", help="Session name (imessage/_15555550100), chat_id, or contact name")

    # kill-sessions
    subparsers.add_parser("kill-sessions", help="Kill all sessions")

    # restart-session
    restart_session_parser = subparsers.add_parser("restart-session", help="Restart a specific session (compacts first)")
    restart_session_parser.add_argument("session", help="Session name (imessage/_15555550100), chat_id, or contact name")
    restart_session_parser.add_argument("--tier", choices=["admin", "partner", "family", "favorite"], help="Override tier for restarted session")
    restart_session_parser.add_argument("--clean", action="store_true", help="Force fresh session (clears session index and resume ID)")

    # restart-sessions
    subparsers.add_parser("restart-sessions", help="Restart all sessions")
    subparsers.add_parser("restart-api", help="Restart the dispatch API server")

    # compact-session (deprecated — compaction is now native)
    compact_session_parser = subparsers.add_parser("compact-session", help="[Deprecated] Compaction is now handled natively by Claude Code")
    compact_session_parser.add_argument("session", nargs="?", help="Session name (ignored — compaction is automatic)")

    # set-model (per-session)
    set_model_parser = subparsers.add_parser("set-model", help="Set model for a session (opus, sonnet, haiku)")
    set_model_parser.add_argument("session", help="Session name (imessage/_15555550100), chat_id, or contact name")
    set_model_parser.add_argument("model", help="Model to use: opus, sonnet, or haiku")

    # set-global-model
    set_global_model_parser = subparsers.add_parser("set-global-model",
        help="Set global model override for all sessions (quota degradation)")
    set_global_model_parser.add_argument("model",
        help="Model: opus, sonnet, haiku, or --clear to remove override")

    # get-global-model
    get_global_model_parser = subparsers.add_parser("get-global-model",
        help="Show current global model override and quota state")
    get_global_model_parser.add_argument("--dry-run", action="store_true",
        help="Show what would happen at current quota levels")

    # install
    subparsers.add_parser("install", help="Install LaunchAgent for auto-start")

    # uninstall
    subparsers.add_parser("uninstall", help="Uninstall LaunchAgent")

    # menubar
    subparsers.add_parser("menubar", help="Start the menu bar app")

    # menubar-install
    subparsers.add_parser("menubar-install", help="Install menu bar LaunchAgent")

    # menubar-uninstall
    subparsers.add_parser("menubar-uninstall", help="Uninstall menu bar LaunchAgent")

    # watchdog-install
    subparsers.add_parser("watchdog-install", help="Install watchdog for auto-recovery")

    # watchdog-uninstall
    subparsers.add_parser("watchdog-uninstall", help="Uninstall watchdog")

    # watchdog-status
    subparsers.add_parser("watchdog-status", help="Show watchdog status")

    # inject-prompt
    inject_parser = subparsers.add_parser("inject-prompt", help="Inject prompt into a session")
    inject_parser.add_argument("chat_id", help="Session name (imessage/_15555550100), chat_id, or contact name")
    inject_parser.add_argument("prompt", nargs="?", default="", help="Prompt text")
    inject_parser.add_argument("--sms", action="store_true", help="Wrap in SMS format")
    inject_parser.add_argument("--admin", action="store_true", help="Wrap in ADMIN OVERRIDE tags")
    inject_parser.add_argument("--app", "--sven-app", action="store_true", dest="app", help="Message from dispatch app (adds 🎤 prefix for voice messages)")
    inject_parser.add_argument("--file", "-f", help="Read prompt from file")
    inject_parser.add_argument("--reply-to", help="GUID of message being replied to (for reply chain context)")
    inject_parser.add_argument("--attachment", help="Path to image attachment for Gemini vision analysis")

    # dispatch-investigation
    di_parser = subparsers.add_parser(
        "dispatch-investigation",
        help="Spawn a background investigation agent from the current session",
    )
    di_parser.add_argument("prompt", nargs="?", default="",
                           help="What you want investigated")
    di_parser.add_argument("--file", "-f", help="Read prompt from file")
    di_parser.add_argument("--from-session", dest="from_session",
                           help="Override originator session (default: detect from cwd)")
    di_parser.add_argument("--allow-mutations", action="store_true",
                           help="Allow Edit/Write tools (default: read-only)")
    di_parser.add_argument("--timeout-minutes", dest="timeout_minutes",
                           type=int, default=15,
                           help="Investigator timeout (default: 15min)")

    # investigation-status
    is_parser = subparsers.add_parser(
        "investigation-status",
        help="Show status of a single investigation",
    )
    is_parser.add_argument("task_id", help="Investigation task_id")

    # investigation-list
    il_parser = subparsers.add_parser(
        "investigation-list",
        help="List in-flight investigations",
    )
    il_parser.add_argument("--from-session", dest="from_session",
                           help="Filter by originating session")
    il_parser.add_argument("--mine", action="store_true",
                           help="Filter to investigations from the current cwd's session")

    # investigation-cancel
    ic_parser = subparsers.add_parser(
        "investigation-cancel",
        help="Cancel a running investigation",
    )
    ic_parser.add_argument("task_id", help="Investigation task_id")

    # auth - SDK auth mode (oauth vs api_key fallback)
    auth_parser = subparsers.add_parser("auth", help="Show or reset SDK auth mode (oauth/api_key)")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", help="Auth subcommands")
    auth_subparsers.add_parser("status", help="Show current auth mode (default)")
    auth_subparsers.add_parser("reset", help="Clear state/auth_mode.json — flips back to OAuth on next daemon restart")

    # health-history - resilience / health-history report (self-healing-resilience §4.4)
    hh_parser = subparsers.add_parser(
        "health-history",
        help="Resilience report: per-dependency state, recovery counts, MTTR, escalations, SLO check",
    )
    hh_parser.add_argument("--hours", type=float, default=24.0, help="Window size in hours (default: 24)")
    hh_parser.add_argument("--dep", help="Filter to a single dependency name (chrome_control, signal_cli, ...)")
    hh_parser.add_argument("--limit", type=int, default=50, help="Max transitions in the tail (default: 50)")
    hh_parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text table")
    hh_parser.add_argument("--db", help="Path to bus.db (default: state/bus.db)")

    # commitment - per-session outstanding-commitment marker (read by the
    # blocked-session watchdog so a stuck task never just idles)
    commitment_parser = subparsers.add_parser(
        "commitment",
        help="Set/clear/show this session's outstanding-commitment marker",
    )
    commitment_parser.add_argument("--session", help="Session name (default: derived from cwd)")
    commitment_subparsers = commitment_parser.add_subparsers(dest="commitment_command", help="Commitment subcommands")
    c_set = commitment_subparsers.add_parser("set", help="Record an outstanding commitment")
    c_set.add_argument("text", help="What you committed to (e.g. 'waiting on the user to reload the extension')")
    commitment_subparsers.add_parser("clear", help="Clear the outstanding commitment (task done / escalated)")
    commitment_subparsers.add_parser("show", help="Show the current commitment (default)")

    # remind - native reminder system
    remind_parser = subparsers.add_parser("remind", help="Manage native reminders")
    remind_subparsers = remind_parser.add_subparsers(dest="remind_command", help="Reminder commands")

    # remind add (default when just using remind "title")
    remind_add = remind_subparsers.add_parser("add", help="Add a reminder")
    remind_add.add_argument("title", help="Reminder title/task")
    remind_add.add_argument("--contact", "-c", help="Contact name, phone, or chat_id (required unless --event is used)")
    remind_add.add_argument("--in", dest="in_duration", help="Fire in duration (e.g., 30m, 2h, 1d)")
    remind_add.add_argument("--at", dest="at_time", help="Fire at time (e.g., 3pm, 15:00)")
    remind_add.add_argument("--cron", help="Cron pattern (e.g., '0 9 * * *' for 9am daily)")
    remind_add.add_argument("--tz", help="Timezone override (e.g., America/Los_Angeles)")
    remind_add.add_argument("--target", "-t", choices=["fg", "spawn"], default="fg",
                           help="Target: fg (foreground session), spawn (new agent)")
    remind_add.add_argument("--event", dest="event_json", help="Event template JSON (generalized mode, no --contact needed)")

    # remind list
    remind_list = remind_subparsers.add_parser("list", help="List reminders")
    remind_list.add_argument("--contact", "-c", help="Filter by contact")
    remind_list.add_argument("--failed", action="store_true", help="Show failed reminders only")

    # remind cancel
    remind_cancel = remind_subparsers.add_parser("cancel", help="Cancel a reminder")
    remind_cancel.add_argument("id", nargs="?", help="Reminder ID")
    remind_cancel.add_argument("--title", "-t", help="Cancel by title")
    remind_cancel.add_argument("--force", "-f", action="store_true", help="Cancel all matching (if multiple)")

    # remind retry
    remind_retry = remind_subparsers.add_parser("retry", help="Retry a failed reminder")
    remind_retry.add_argument("id", help="Reminder ID")

    # remind next
    remind_next = remind_subparsers.add_parser("next", help="Preview next fire times for cron pattern")
    remind_next.add_argument("pattern", help="Cron pattern")
    remind_next.add_argument("--tz", help="Timezone (default: system)")
    remind_next.add_argument("-n", type=int, default=5, help="Number of times to show")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
        "attach": cmd_attach,
        "monitor": cmd_monitor,
        "kill-session": cmd_kill_session,
        "kill-sessions": cmd_kill_sessions,
        "restart-session": cmd_restart_session,
        "restart-sessions": cmd_restart_sessions,
        "restart-api": cmd_restart_api,
        "compact-session": cmd_compact_session,
        "set-model": cmd_set_model,
        "set-global-model": cmd_set_global_model,
        "get-global-model": cmd_get_global_model,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "menubar": cmd_menubar,
        "menubar-install": cmd_menubar_install,
        "menubar-uninstall": cmd_menubar_uninstall,
        "watchdog-install": cmd_watchdog_install,
        "watchdog-uninstall": cmd_watchdog_uninstall,
        "watchdog-status": cmd_watchdog_status,
        "inject-prompt": cmd_inject_prompt,
        "commitment": cmd_commitment,
        "health-history": cmd_health_history,
        "remind": cmd_remind,
        "auth": cmd_auth,
        "dispatch-investigation": cmd_dispatch_investigation,
        "investigation-status": cmd_investigation_status,
        "investigation-list": cmd_investigation_list,
        "investigation-cancel": cmd_investigation_cancel,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
