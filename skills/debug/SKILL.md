---
name: debug
description: Debug and diagnose the Dispatch personal assistant system. Troubleshoot sessions, trace messages, find incidents, check daemon health.
---

# /debug — Dispatch Diagnostics

## Quick Start

```bash
debug status                          # System dashboard
debug trace <phone> --since 1h        # End-to-end message trace
debug session imessage/<phone>        # Session deep-dive
debug incident --since 2h            # Auto-scan for anomalies
debug routing <phone>                # Contact routing info
```

All commands support `--json` for machine-readable output, `--verbose` for more detail, `--quiet` for terse output.

## CLI Reference

| Command | Description |
|---------|-------------|
| `debug status` | Daemon PID/uptime/memory, watchdog, signal, sessions table, bus stats, open FDs |
| `debug trace <chat_id>` | Message journey: chat.db -> bus -> session -> response. Timeline with latency |
| `debug session <name\|chat_id>` | Session info, recent bus events, sdk_events, log tail, compaction info |
| `debug incident` | Auto-detect: failed messages, crashed sessions, heartbeat gaps, restart loops, errors |
| `debug routing <chat_id>` | Contact lookup, tier, session assignment, transcript dir, backend |

## Runbooks

### Session Crashed
```bash
debug incident --since 30m                    # Find crash events
debug session <name>                          # Check session state & logs
# If session exists but is dead:
claude-assistant restart-session <name>
# If repeated crashes:
debug trace <chat_id> --since 2h              # Find what triggered it
```

### Message Not Delivered
```bash
~/dispatch/bin/bus-audit                      # CHECK THIS FIRST — dead letters
debug trace <chat_id> --since 1h              # Find the gap
debug session <name>                          # Is session alive?
# Check if message.received exists but no message.sent:
# Gap between received and injected = routing issue
# Gap between injected and turn_complete = session stuck
# No message.sent after turn_complete = Claude didn't call send-sms
```

**Check `messages.dlq` before anything else.** A message that failed to
process is retried twice and then parked in `messages.dlq`. Nothing consumed
that topic until 2026-08-26, so a failed delivery and a message the user never
sent looked identical — from their side AND from ours. Eight real messages sat
there for six weeks; two of them were the admin saying "stop", which is why he
had to say it three times.

There is no error anyone sees for this. It presents as silence, and silence is
also what "everything is fine" looks like. `bus-audit` is the only thing that
distinguishes them.

> **`debug incident` is blind to anything older than the live table.**
> Every query in this CLI reads `FROM records` — 15 sites, zero references to
> `records_archive`. Live holds 34k rows back to ~30 days; the archive holds
> 221k back to Apr 28. So `debug` sees roughly **13% of the bus**, and
> `--since 60d` silently returns a shorter list rather than erroring or
> warning. It *did* correctly flag the Aug 19 drops as `CRIT message.failed`
> — it simply cannot see the July ones at all.
>
> **Do not schedule `debug incident` until it reads `records_archive`.** A
> nightly run today would report clean while the evidence sits in a table it
> cannot query — which is worse than no detector, because it exits 0.
>
> For anything older than ~30 days use FTS, which *does* index the archive:
> ```bash
> sqlite3 ~/dispatch/state/bus.db "select datetime(timestamp/1000,'unixepoch','localtime'), topic, substr(payload_text,1,120) from records_fts where records_fts match 'your search' order by 1;"
> ```
> FTS also shows the retry attempts preceding a dead-letter, which the
> `messages.dlq` row alone does not.

### Daemon Unresponsive
```bash
debug status                                  # Check PID, memory, FDs
# If PID exists but high memory/FDs:
claude-assistant restart
# If no PID:
claude-assistant start
# Check watchdog:
~/dispatch/bin/watchdog-status
```

### Slow Performance
```bash
debug status                                  # Check resource usage
debug trace <chat_id> --since 1h              # Find latency bottlenecks
# Check perf logs:
tail -20 ~/dispatch/logs/perf-$(date +%Y-%m-%d).jsonl | python3 -m json.tool
```

### Restart Loop
```bash
debug incident --since 1h                     # Detects restart loops automatically
debug session <name>                          # Check consecutive error count
# Look for session.created events close together:
# If 3+ restarts in 5 min = restart loop
# Common cause: context_length_exceeded, image_too_large
```

### Stuck Session
```bash
debug session <name>                          # Check last_inject vs last_response
debug trace <chat_id> --since 30m             # Find where it got stuck
# session.injected with no sdk.turn_complete = stuck
claude-assistant restart-session <name>
```

### Mac Lost IPv4 (169.254 self-assigned on en1)
Symptoms: `ifconfig en1 | grep 'inet '` shows 169.254.x, `ping 1.1.1.1` fails, but IPv6 still works, so iMessage/Signal keep running and the outage is easy to miss. Craigslist and LAN devices (Hue/Lutron/Sonos) break.
Seen 2026-10-15/16: the Xfinity gateway's DHCPv4 stopped answering while DHCPv6 kept working. `networksetup -setdhcp Wi-Fi` and toggling manual→DHCP did nothing.
```bash
# Workaround that needs no human and no Wi-Fi power-cycle (IPv6 stays up throughout):
networksetup -setmanual Wi-Fi 10.0.0.140 255.255.255.0 10.0.0.1   # LAN is 10.0.0.0/24, gateway 10.0.0.1, .140 was this Mac's old lease
ping -c2 1.1.1.1 && curl -4 -sI https://www.craigslist.org | head -1
# Undo once DHCP works again:
networksetup -setdhcp Wi-Fi
```
Don't power-cycle Wi-Fi (`-setairportpower`) without the admin's OK, because the Mac may not rejoin and goes fully dark. A daily reminder tested whether DHCP was back. It came back on 2026-10-19 (a real 48h lease from 10.0.0.1, after Eric got home), and the static IP was removed.

## Reporting Findings — Measurement Discipline

Read this before stating any number in a bug report. On 2026-08-25/26 a single
investigation produced **four** wrong findings. None was a hallucination or a
bad inference. Every one was a **true statement about the wrong set**.

**Never report a count you did not ask the machine to count, and always say
what you counted over.**

A wrong number is never malformed. The cheap query and the correct query return
the same *shape* — an int, a list of rows, a file list — so nothing in the
output tells you which one you ran. This is why "go look" catches these and
"think harder" does not.

The four ways it went wrong, all real:

| Failure | What happened |
|---|---|
| **Counted by eye** | Read `DEVNULL` hits off a `head`-limited grep. Reported 3; it was 15. |
| **Wrong corpus** | Machine-counted correctly over a tree including `logs/` and `.venv`, which *mention* the pattern rather than *use* it. 40 vs 25 vs 15 vs 6 — all exact, all different questions. |
| **Wrong table** | Counted `records` and skipped `records_archive`. Under-reported the DLQ 5 vs 8. Both queries return rows; neither errors. |
| **Wrong scope, then compared** | Reported "15" (assistant/+bin) against another box's "9" (repo-wide) as if it were a comparison. Two correct integers side by side, measuring different sets. |

Corollaries:

- **A proxy is not the thing.** `ls Sources/ | wc -l` is not
  `_get_all_addressbook_dbs()`. Grepping `assistant/*.py` is not "the codebase"
  — `bin/email-producer` was sitting right there.
- **Checking one side is not checking the system.** A consumer with direct
  `att['name']` subscripts is unsafe even when the producer happens to emit
  `name`. Say *structurally exposed, empirically safe* — do not say "we don't
  have it."
- **State the scope in the sentence**, not in your shell history, so a reader
  can check it. Same principle as making output state its own reasoning: a
  claim that carries its own qualifier can be caught being wrong.
- **Report negatives plainly.** "Four of five transferred, the fifth didn't" is
  worth more than a fifth finding dressed up to match.
