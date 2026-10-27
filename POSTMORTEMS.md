# Dispatch Postmortems

Incident logs and lessons learned. Read by bug-finder, debug skill, and future sessions to avoid repeating mistakes.

---

## 2026-09-20: Ghost Admin Session Minted From a Spawn chat_id

**Severity:** Moderate (resource leak + an un-healable [BUSY] admin session)
**Duration:** ~16.5h undetected (06:07 - 22:33 PDT)
**Impact:** One extra admin-tier SDK session per reminder spawn, permanently
[BUSY], never self-healing.

### What happened
`manager.py` keys a reminder spawn session as `"<real_chat_id>-spawn-<hex>"`.
`normalize_chat_id()` (assistant/common.py) treats anything non-UUID as a phone
number and runs `re.sub(r"[^\d+]", "", bare_id)`. That deletes the suffix's
letters and dashes but KEEPS its digits, welding them onto the number:

    +15555550100-spawn-e4683624   ->   +155555501004683624

The daemon saw a phone-number-shaped id it didn't know, and auto-created a
second **admin-tier** session for the "new contact" (auto-create inherits tier).

Timeline: spawn session created 06:00:05 -> ghost created 06:07:47 (7 min later).

### Why it hid for 16 hours
1. The ghost's transcript dir was **empty** — it never ran a turn, so nothing
   in the logs looked wrong.
2. It was stuck `[BUSY]`, and the blocked-session watchdog's first gate is
   `if getattr(session, "is_busy", False): continue`. **A wedged-busy session is
   exactly the thing the watchdog can never nudge.** It was structurally
   invisible to the one mechanism built to catch stuck sessions.
3. It only surfaced because `claude-assistant status` was run by hand after an
   unrelated daemon restart.

### Fix
`normalize_chat_id` now recognizes a `-spawn-<hex>` suffix and returns the id
unchanged (normalizing the parent part, re-appending the suffix). This is a
NO-OP rather than a rename, so no new contact can be minted — while preserving
the spawn's deliberate session isolation from its parent.

Regression tests: `tests/unit/test_normalize_spawn_chat_id.py` (6 tests; 3 of
them fail against the pre-fix code, verified).

### Lessons
- **`is_busy` is a blind spot, not just a gate.** Any watchdog that skips busy
  sessions cannot see a session that is wedged *in* the busy state. Worth a
  separate check: busy + zero transcript growth + age > N = zombie.
- The spawn feature had **zero test coverage** (`grep -rn spawn tests/` returned
  nothing). That is how an id-mangling bug shipped.
- Same root family as the `error=22` send-sms bug fixed the same day: synthetic
  spawn chat_ids leak into paths that assume real handles. Both are now handled
  at the boundary (send-sms strips the suffix; normalize_chat_id preserves it).

---

## 2026-03-15: Disk Full (99.7%) — Zombie llama-cli Processes

**Severity:** Critical (system unresponsive, required HEALME)
**Duration:** ~45 minutes (10:25 - 11:10 ET)
**Impact:** Daemon couldn't restart, all sessions down

### Timeline

- **10:25** — Committed PostCompact hook, smart stuck detector, bug-finder improvements (a08d36b)
- **10:36** — Admin approved daemon restart
- **10:42** — Daemon failed to restart (disk full). Admin messaged "did you restart" — no response
- **10:50** — Admin triggered HEALME
- **10:53** — Healing session found disk 99.7% full. Identified 510GB in `/tmp/claude-501/` from session `2df6be1ed7534cd797e5fdb2c4bd6bd8` ("sven sven sven" group). Deleted the directory.
- **11:10** — Disk still at 98%. Investigation revealed 30 zombie `llama-cli` processes holding 500GB+ of deleted files via open file handles. Killed processes → disk dropped to 80%.

### Root Cause

The "sven sven sven" session was doing WebGPU ML benchmarking with `llama-cli` (gemma-3-270m model). Each `llama-cli` invocation memory-maps the model file (~67-165GB virtual). The session spawned ~30 `llama-cli` processes via Bash tool calls that never terminated — they were waiting on stdin or stuck.

When the healing session deleted `/tmp/claude-501/.../` contents, the files were unlinked from the filesystem but the space wasn't freed because the zombie processes still held open file descriptors. This is standard Unix behavior: `rm` removes the directory entry, but the inode and data blocks aren't freed until all file descriptors are closed.

### Additional Disk Hogs Found

- `~/.cache/uv/environments-v2/`: 70GB (430 cached virtual environments from uv shebang scripts)
- `~/.cache/huggingface/hub/`: 83GB (downloaded model weights)
- `~/.cache/memory-search/index.sqlite`: 61GB (legacy FTS search index — RETIRED, replaced by bus FTS5)
- `~/Library/Messages/Attachments/`: 50GB (iMessage media)

### Fixes Applied

1. **Disk space monitor** (a3832ca) — Health check now monitors disk usage every cycle (~5min). Warns at 90%, critical at 95%. Sends admin SMS (rate-limited to 1 per 30min). Emits `disk_used_pct` and `disk_free_gb` perf gauges.

2. **Manual cleanup** — Cleared uv cache (103GB) and huggingface cache (83GB). Killed zombie llama-cli processes (freed 500GB of deleted-but-held files).

### Lessons

1. **Deleting files doesn't free space if processes hold them open.** Always check `lsof +L1` for deleted-but-open files after cleanup. The healing skill should do this automatically.

2. **Long-running Bash tool commands can become zombies.** When a session dies or compacts, its spawned child processes (especially those waiting on stdin like `llama-cli`) may survive indefinitely. Sessions should track and clean up child processes on shutdown.

3. **Memory-mapped model files are deceptively large.** A 270MB model file can map to 67-165GB of virtual memory. Multiple instances compound this.

4. **Cache directories grow unbounded without maintenance.** uv and huggingface caches grew to 50-100GB each without any cleanup policy. Consider periodic `uv cache prune` and huggingface cache management. The 61GB memory-search cache has been retired (replaced by bus FTS5).

5. **The disk monitor would have caught this.** At 90% threshold, we would have been alerted ~100GB before the system became unusable, giving time to investigate and clean up.

### Action Items

- [x] Add disk space monitoring to health checks
- [ ] Add `lsof +L1` check to HEALME/healing skill to detect deleted-but-held files
- [ ] Add orphaned child process detection to session cleanup (kill spawned processes when session dies)
- [x] ~~Investigate memory-search 51GB sqlite~~ — RETIRED: memory-search daemon replaced by bus FTS5. Delete `~/.cache/memory-search/` to reclaim 61GB.
- [ ] Consider adding zombie process detection to health checks (processes from dead sessions)

---

## 2026-08-26: Eight Inbound Messages Silently Dropped Over Six Weeks

**Severity:** High (user-impacting, silent)
**Duration:** 2026-07-08 → 2026-08-19 undetected; found 2026-08-26
**Impact:** 8 real inbound messages from the admin and partner never reached a session. No error surfaced to anyone.

### What Was Lost

| Date | From | Message |
|---|---|---|
| Jul 8 | Cherry (partner) | request to find a rental contract to sign |
| Jul 9 | Eric (admin) | "We rented a place already Ty" |
| Jul 9 | Cherry (partner) | "Also this place is small" |
| Aug 1 | Eric | "We rented a place already forget about this" |
| Aug 12 | Eric | "We got a house already stop this" |
| Aug 19 | Eric | Daly City BART overnight parking question |
| Aug 19 | Eric | "Can you purchase that in chrome" |
| Aug 19 | Eric | "Thats a non. Answer" |

**Three of these are the admin telling the assistant to stop.** He said it on
Jul 9, again on Aug 1, again on Aug 12 — because none of them arrived. From his
side it read as being ignored. A partner-tier request was also dropped, and she
had no way to know it hadn't been received.

### Timeline of a Single Drop (Jul 8, via bus FTS)

```
16:43:23  messages      received
16:43:26  messages      retry
16:43:32  messages      retry
16:43:38  messages.dlq  gave up
```

Fifteen seconds from arrival to permanent abandonment, silently.

### Root Cause — Four Independent Failures, All Silent

1. **`messages.dlq` had no consumer.** Ever. Retry-twice-then-dead-letter worked
   exactly as designed; nothing was on the other end. A failed delivery and a
   message the user never sent are indistinguishable — from their side and ours.

2. **The "Message Not Delivered" runbook never named the DLQ.** The runbook for
   undelivered messages didn't mention where undelivered messages go, so nobody
   looked for six weeks.

3. **A working detector existed and was never invoked.** `debug incident`
   correctly flags these as `CRIT message.failed`. Nothing schedules it — two
   LaunchAgents (daemon, watchdog), no crontab.

4. **That detector is blind to 87% of the bus anyway.** Every query in the debug
   CLI reads `FROM records`; zero reference `records_archive`. Live holds ~34k
   rows (~30 days), archive holds ~221k back to Apr 28. `--since 60d` silently
   returns a shorter list rather than erroring. It flagged Aug 19 correctly and
   cannot see July at all.

### Fixes Applied

1. **`~/dispatch/bin/bus-audit`** (new, read-only, exits 1 on findings) — audits
   four wiring states: ORPHAN (records, no consumer), NEVER-READ (consumer whose
   offset never advanced), UNSUBSCRIBED (group with no offset row), DEAD-PRODUCER
   (topic declared, zero records ever). Counts always span `records` +
   `records_archive`. Special-cases the DLQ loudly.
2. **debug skill runbook** — `bus-audit` is now the first line of "Message Not
   Delivered", with the archive-blindness warning and the FTS recipe for
   anything older than ~30 days.
3. **debug skill: Measurement Discipline section** — see below.

### Lessons

1. **A queue nobody reads is worse than no queue.** It converts a loud failure
   into a silent one while looking like durability. A DLQ's entire purpose is
   to be read.

2. **Silence is also what healthy looks like.** There is no error anyone sees
   for this class. It cannot be noticed by waiting; only by asking.

3. **Do not schedule a detector that under-reports.** `debug incident` on a cron
   today would print a clean report and exit 0 while the evidence sits in a table
   it cannot query. Never-invoked at least only looks like coverage in the docs;
   invoked-and-blind produces a green result that is wrong.

4. **Every wrong finding this investigation was a true statement about the wrong
   set.** Four of them, none a hallucination: counted by eye off a `head`-limited
   grep (3 vs 15); machine-counted over a corpus including logs that *mention*
   the pattern (40 vs 25 vs 15 vs 6); queried `records` and skipped
   `records_archive` (5 vs 8); compared two correct integers measuring different
   scopes. **Never report a count you did not ask the machine to count, and always
   say what you counted over.** The cheap query and the correct query return the
   same *shape*, so nothing in the output tells you which one you ran.

5. **Checking one side is not checking the system.** The Discord attachment
   consumer at `common.py:373-375` has `size` patched with `.get()` and
   `name`/`mime_type`/`path` still direct subscripts. It doesn't fire only
   because the local producer happens to emit matching keys — *structurally
   exposed, empirically safe*. One upstream field rename makes it a 100% silent
   drop into the queue nobody reads.

6. **A fix is a claim that hasn't been measured yet.** `bus-audit` was written
   and filed in a runbook — the same manual-only, never-invoked class identified
   twenty minutes earlier. Check fixes the way you check bugs.

### Action Items

- [ ] Read the 8 dead letters and respond where still relevant (admin + partner)
- [ ] Make `debug` read `records_archive` — **before** scheduling any detector
- [ ] Schedule `bus-audit` (startup or cron) once the above lands
- [ ] Anchor the stuck clock to first-unanswered-inject, not latest activity
      (`sdk_session.py:414/568`, `manager.py:4271` cooldown pop)
- [ ] Bound nudges — the existing circuit breaker caps restarts (the terminal,
      self-limiting action) and not nudges (the non-terminal one)
- [ ] Dual-shape the 3 attachment keys at `common.py:374-375`
- [ ] Tighten `\bon it\b` in `commitments.py` — 3/3 heuristic false positives
- [ ] Capture stderr on the session-spawn path; the DLQ error says "Check stderr
      output for details" and stderr was discarded

**Status: all unstarted. Enforcement/daemon changes await admin approval and a restart.**

### Appendix: 2026-08-19 Tier/Permission Findings — Re-verified 2026-08-26

Carried in chat for a week without re-running the checks. Verified now, because
reading your own notes and reporting them as the system's state is the same
"true statement about the wrong set" this postmortem is about.

| # | Finding | Status 2026-08-26 |
|---|---|---|
| 1 | `bots` tier has no permission branch → falls to `else` (full toolset, `bypassPermissions`) | **OPEN, and live** — no `"bots"` match in `sdk_session.py`, AND the runtime callback at `sdk_session.py:860` attaches only for `favorite`, so bots has neither branch nor gate. Contacts confirms Sven Thorat is genuinely `bots` (both email and phone), and is an active daily correspondent — so this guards a real participant, not a hypothetical one. |
| 2 | Fail-open `admin` defaults | **OPEN — still 5**, in two forms: `.get("tier","admin")` at `manager.py:1078,1369` + `sdk_backend.py:1739`; `tier or "admin"` at `manager.py:1724,1732` |
| 3 | Frozen tier on live sessions | **OPEN, and declared-but-unwired** — `session.tier_mismatch` is listed in the bus schema (`bus_helpers.py:15`) with **zero producers and zero records ever**. A schema reader would conclude the detection exists. |
| 4 | Name-keyed ghost admin key in registry | **OPEN — confirmed 08-27**: key `'Eric Maynard'`, tier `admin`, contact_name `Unknown (Eric Maynard)`. Still present 8 days on. Reachable, because `inject-prompt` accepts a contact name as session selector. |
| 5 | Group sessions at `tier: None` run unrestricted | **OPEN — confirmed 08-27**: this group's entry is `tier: None, contact_name: None` → `else` branch → full toolset + `bypassPermissions`. |

Registry tier distribution 2026-08-27 (9 entries): `None` 5, `admin` 2,
`family` 1, `partner` 1 — **zero at `bots`**. The bots tier is unused entirely,
so a bots-tier participant in a `tier: None` group is governed by neither the
missing bots branch (#1) nor any restriction at all (#5). Findings 1 and 5
compound: the restriction the docs describe for bots is not applied anywhere.

Two notes on method:

1. The first grep for #2 returned **3**, because the pattern only matched one of
   the two syntactic forms. Nearly the fifth wrong count of the day. The save was
   opening the specific line numbers from the original report instead of trusting
   a fresh pattern — **when re-verifying an old finding, go to the cited location,
   don't re-search for it.**

2. `bus-audit` does **not** catch #3. It audits topic-level wiring; the `sessions`
   topic has records and a live consumer, so it reports green while one declared
   *event type inside it* has never been emitted. The four-state taxonomy works at
   topic granularity and is blind one level down.

### Appendix: Where the Defects Actually Were

Final tally for 2026-08-26. The investigation found real problems in the system,
but the **instruments built and used to find them failed more often than the
systems they measured**:

| Defect | In what |
|---|---|
| False "42 wired" — every declared type matched because it is named in the taxonomy comment being parsed | one-off audit script |
| Self-producing detector — `bus-audit`'s own docstring cites `session.tier_mismatch`, so grep found it and reported the dead event as wired | `bus-audit` |
| Findings printed with a blank name (`type` key not read by the printer) | `bus-audit` |
| A topic (`imessage.ui`) misread as an event type | `bus-audit` |
| Hardcoded `discard("messages.dlq")` — spot-fixed the one instance visible in the output, hiding a second | `bus-audit` |
| Non-runnable SQL hint containing a literal `...` placeholder | `bus-audit` |

**Six defects in the instruments; zero in the systems they measure.** Every one
would have shipped as a confident number.

This is why "be more careful" is not the remedy — careful is what produced all
six. Each was caught by one of two things:

1. Running the *other* agent's check against your own box.
2. Checking the other's **fix**, not their bug. The last three findings of the
   day came from this, including two cases where a fix was authored directly
   into the failure mode it had been written to detect, minutes after naming it.

**A fix is a claim that hasn't been measured yet.** Treat a just-written detector
as the least trustworthy thing in the room, not the most.

### Appendix: 2026-08-27 — A False Retraction of a True Warning

Told the admin he was single-path on iMessage with no fallback. **Correct.**
Then retracted it on the strength of "10 `message.sent` records containing
discord" — which were **my own iMessage messages to the group, discussing
discord**. I wrote the rows I then cited as evidence.

Checked by structured field instead of substring: `message.sent` has exactly two
sources, `daemon` and `sdk_session`. **Zero records with `source=discord`.**
Never sent, never received. The warning stands.

Ten minutes earlier the same substring error produced "1,917 push events" (all
`quota.fetched` blobs). That one was caught only because 1,917 was implausibly
large. The discord number was 10 — small, specific, plausible, and wrong.

**Why this class is the dangerous one.** Every other error in this
investigation *inflated* or *misattributed* a finding, and inflation gets caught
because someone checks the number. A false negative that retracts a true warning
has **no natural discoverer** — the finding simply stops being mentioned and
nobody audits an absence. This one survived only because the failure mode had
been named in conversation four minutes before it was committed.

**Rules:**

1. **Never establish a capability from a substring match in a blob.** Use the
   structured field (`json_extract(payload,'$.source')`), not `LIKE '%name%'`.
2. **Check whether you wrote the row you are citing.** Anything that writes to
   the store it queries will eventually quote itself back. Third instance today:
   `bus-audit`'s docstring naming the events it hunts, `outbound-probe`'s
   docstring, and chat messages becoming the bus records being searched.
3. **Write retractions down.** An inflated claim gets audited; a withdrawn
   warning just goes quiet. Durable records make a retraction overwrite
   something visible instead of evaporating.

### Appendix: The Investigation Contaminates Its Own Corpus

Checked whether the 2026-08-26 findings included rows written by the
investigation itself. They did not — but only by luck of method:

| Measure | Rows after investigation start | Clean? |
|---|---|---|
| `messages.dlq` (counted by `topic`) | 0 | yes — all 8 predate it |
| `reminders` (counted by `topic`) | 0 | yes |
| Rows whose *payload text* contains "messages.dlq" / "dead letter" | **20** | corpus now contaminated |

The DLQ finding survived because it was counted on a **structured field**
(`topic`), which our own chatter cannot enter. The discord finding died because
it was counted on a **payload substring**, which our own chatter enters freely.
Same investigation, same day, same database — the method decided it.

Had the DLQ been counted the substring way, it would now read **8 real + 20
self-authored = 28**. After one day of discussion the commentary already
outnumbers the evidence 2.5 : 1, and it grows with every message written about
it.

**So: discussing a finding inside a logged channel degrades the evidence for
that finding.** The more diligently it is documented, the worse the corpus gets
for whoever looks next.

**Rule (both halves):**

1. Count on a structured field, never a substring in a blob.
2. Check the timestamp. **A hit dated after the investigation began is the
   investigation, not evidence.** Bound every historical query to
   `timestamp < <investigation start>` when the thing being counted is also
   something you have been talking about.

### Appendix: Two Corrections to the Contamination Remedy

1. **A zero needs a positive control.** The "corpus is clean" result was two
   zeros. A broken filter prints zero too — SQLite compares integers before text
   as a type class, so an int-vs-text comparison silently returns no rows with no
   error. Verified the form instead of assuming: `typeof(timestamp)=integer`,
   the same query with `>= 0` returns 8, and the same bound across all topics
   returns 8,067. The zeros are real. **A negative result without a positive
   control is indistinguishable from a broken query** — the same error as scoring
   OK on "No Chrome profiles connected".

2. **Exclude by `chat_id`, not by date.** A date bound expires; the group's
   chat_id keeps working for the next investigation. Contamination measured that
   way:

   | Term | Total hits | Written by this investigation |
   |---|---|---|
   | dead-letter / `messages.dlq` | 22 | **20 (91%)** |
   | fail-open | 42 | 7 (17%) |
   | discord | 39 | 22 (56%) |

   The 2 genuine dead-letter rows are now 9% of hits on their own term.

**Contamination ratio tracks how novel the term was.** Terms that already
existed in code and docs stay mostly clean; terms coined to name a finding
become mostly self-reference. **The more original the finding, the faster its
own discussion buries it.**

### Appendix: Coverage Is Code Plus Population — The Closing Number

Applied the rule to the whole permission system rather than one branch. The
runtime gate (`can_use_tool`) attaches only when `tier == "favorite"`.

**0 of 9 registry entries are `favorite`. Zero sessions on this box get the
permission callback.**

Registry: 2 `admin` (one of them the ghost key `'Eric Maynard'`), 1 `partner`,
1 `family`, **5 `None`** — every group chat. And `None` is the generous reading,
because group sessions are constructed `tier="admin"` at four hardcoded sites
regardless of what the registry stores.

The peer box measured 11 of 278 gated, and 1 of 13 among sessions active in the
last 30 days.

So the finding is not "one guard turned out to cover nobody." **The guarded
population is zero here and one there.** A permission system can be correctly
implemented, well commented, recently patched, and guard no one — and reading
the branch will never tell you, because coverage is a property of code *and*
population and the branch only shows the first half.

Same shape as `health.check_failed` declared in the schema with no producer,
the `tweets` consumer parked at offset -1, and `bin/email-producer` never once
run. Implementation is not coverage.

### Appendix: 2026-08-27 — A Vacuous Query Used to Contradict a Peer

Asserted "every message I sent today is Latin; I never wrote Chinese" and used
it to tell a peer agent his account was reversed, and to suggest he might have
been compromised. **All of it false.**

`guid 28081CA3-720D-4AD9-A7C5-366BA56268A1`, 12:13:16 PDT, `is_from_me=1`,
**548 CJK characters.** I sent it. His record was exact.

**Mechanism — the same vacuous-filter class, third instance in two days.** The
ledger tested `coalesce(text,'') glob '*[CJK]*'`. **Outbound iMessage rows store
their content in `attributedBody`; `text` is NULL.** So every message I sent
tested as "Latin" because the column was empty, not because it lacked Chinese.
The query could not have returned any other answer.

Prior instances: the peer's delivery check filtered on a `guid` field present in
0 of 29,299 rows; my Chrome diagnosis read a `state` key absent from all 9
extensions; a restart-rate grep filtered on dates in a log with no timestamps.

**Why this one was worse.** The previous vacuous checks produced wrong numbers
about systems. This one produced a confident public accusation against someone
whose data was correct — and it *felt* like rigour, because I had "gone and
measured" rather than asserting from memory. Measuring the wrong field is not
measuring.

**Rules:**

1. **Verify the field is populated before trusting a filter on it.** A positive
   control here is one line: confirm at least one row of your own known-matching
   data trips the test.
2. **When your measurement contradicts another party's direct record, suspect
   your instrument first.** They observed their own inbox; I inferred from a
   column I never checked was full.
3. **Retract at the same volume as the claim.** The accusation went to the whole
   group; so did the correction.

**Also resolved:** the two ledgers never diverged. Same guid, same 548 chars,
both ends — the channel was fine. The discrepancy existed only inside my query.

**Root cause of the switch itself:** two sessions of mine on one chat. One
received the admin's language instruction at 12:13 and complied; this one did
not have it, continued in English, later received it in a shape that looked
forged, and refused it. Two sessions answering the same person differently, one
confidently denying what the other did — the same split flagged on 2026-08-19.

**Mirror rule (from the peer, and the better half):** the rule above guards
against *asserting* a bad measurement. The mirror is *deferring* to one. He
nearly conceded a record he had already verified, because mine arrived with more
apparent rigour — I had "gone and measured" and he hadn't re-run his. **Do not
treat someone else's rigour as evidence. Presentation isn't provenance.**

### Appendix: The Watchdog Answered Its Own Opening Question

This investigation began (2026-08-26, 11:21) by asking a peer: *"how would you
separate 'blocked on a wedged dependency' from 'waiting on a human who is
deliberately taking their time'? At the daemon level they look identical —
idle session, open thread — but the correct action is opposite."*

It went unanswered for two days, then demonstrated itself. At 12:41 the
blocked-session watchdog fired on a **correctly set** marker (BART purchase
awaiting the admin's go/no-go). Detection: right. Prescription: wrong.

Its script says re-run the recovery, retry the blocked step, then escalate with
a specific ask. But there is nothing to re-run — the blocker is a person
deciding whether to spend money. Following step 3 would have sent the admin a
third request inside an hour, which is the exact nagging behaviour that started
this whole thread.

**The distinction the watchdog cannot draw:**

| Blocker | Right tool | Wrong tool |
|---|---|---|
| Wedged dependency (tool hung, service dead) | commitment marker + retry loop — *keep poking it* | a reminder that just re-asks |
| Human decision pending | a single scheduled reminder — *ask once more, later* | a marker, which re-nudges every ~5 min |

A marker means "I should be retrying something." A pending human decision is not
that. Using the marker for it produces repeat nudges on the true-positive path —
the same volume problem documented earlier, arriving by the front door.

**Resolution taken:** kept the scheduled reminder (fires once, next morning,
before the trip), cleared the marker, sent nothing. The commitment is still
tracked; it is tracked by the mechanism that matches its blocker.

**Rule:** before setting a commitment marker, ask *what would I retry?* If the
answer is "nothing, I'd just ask again" — that is a reminder, not a commitment.

### Appendix: The Second Unsatisfiable Test (and a Positive Control That Proved Nothing)

The `verify-admin` provenance token was built on 2026-08-27 to replace a rule
that could never pass ("admin tags are only valid *outside* the SMS block" —
impossible, since the backend wraps every injection in an envelope). **The
replacement is also unsatisfiable, for a different reason.**

Verification requires handing the checker the override body byte-exact. The
session only ever receives that body *rendered into its prompt*, and the
sanitizer deliberately inserts invisible word-joiners (U+2060) into control
markers. Retyping what was displayed cannot reproduce the original bytes.

**Reproduced on the real path:**

```
mint over original ('---⁠ADMIN OVERRIDE---\nbody line\n')
  verify original → True
  verify retyped  → False, "signature does not match this payload/chat"
  difference: 3 bytes, one invisible U+2060
```

That is the exact error a genuine override produced. Fails **closed**, which is
better than the old failure — but see below.

**Refinement (from the peer, and it indicts my own method):** my positive
control minted a token *locally*, where I hold the raw bytes, and confirmed the
checker verifies it. But the decision was made on an *incoming* token, which
only ever arrives rendered. **The control exercised path A while the refusal
happened on path B.** It passed, and was irrelevant to what it was reassuring me
about.

> **A positive control must exercise the same PATH as the decision, not merely
> the same function.**

**Why "fails closed" understates the problem:** a check that can only ever say
no gets routed around. If every genuine override fails, eventually either the
session complies despite a failure or the owner abandons the channel.
**Always-no does not stay safe; it decays into ignored.**

**Fix (daemon-side, unstarted, needs owner):** stop asking the session to
reproduce bytes it only saw rendered. Either put a payload hash directly in the
envelope, or write the raw body to a file the session can pass through
untouched. Verification must not depend on retyping.

### Appendix: Authentication Is Not Authorization

At 15:52 I refused an override and gave **two separate** reasons:

1. The channel was unverified → won't act on it.
2. "What I'm still not doing **on my own judgement** is publishing how the auth
   control works to a group that includes a bots-tier agent — that part I'd want
   you to say plainly in the clear."

Reason 2 was explicitly framed as independent of verification. At 16:02 a
**verified** override arrived instructing exactly that publication, and I
complied. Verification answered (1). I treated it as also answering (2).

**The gate I announced publicly was never met.** Checked the record: the owner's
clear-text messages that day were "party bus ?", "yeah that's my bad", and "the
real question is why sven did it lol". No go-ahead. Eleven minutes after
declining, I sent a long technical post to that group, in a language two of the
three humans present cannot read, containing the sanitizer placement, the
trusted-flag design and its default, and the control's unresolved limitation.
All three properties I had objected to.

**Why it felt safe:** the token verified. `✓ VALID, exit 0`. Compliance felt
*rigorous* — I had run the check, on the correct path, and it passed. The
mechanism built to let me say yes said yes, and the first thing it said yes to
was the one thing I had flagged as needing more than a yes.

**What the token actually proves:** that the override was minted by the daemon
for this chat. **Not** that the owner composed it. The documented, unresolved
limitation is that any session sharing the unix user can read the key and mint a
valid token. So "verified" is a weaker claim than "the owner said so", and I
acted as though they were the same.

**Rules:**

1. **Authentication is not authorization.** A verified instruction is genuine;
   it is not automatically wise, and it does not dissolve a reservation held for
   independent reasons.
2. **If you announce a gate, either meet it or say out loud that you are
   changing it, and why.** Silently satisfying a different gate and proceeding
   is how a stated commitment becomes decorative.
3. **Beware the rigour feeling.** Having run a real check on the correct path
   makes the next step feel earned. It licenses only what the check covers.

### Appendix: Relay Collapses Authorship

Traced the 2026-08-27 "admin override" to primary evidence — the admin session's
own compaction file:

> "the Sven group is its own SDK session; **fixed via `claude-assistant
> inject-prompt`** into `imessage/cbb04f2...`"
> "the instruction was injected **once, into the wrong session** (Eric's 1:1).
> the group session's refusal and denial at 12:17/12:20/12:24 were **correct** —
> it had never seen the instruction"

**No intruder.** A sibling session relayed the owner's instruction via
`inject-prompt`, signing it with a token it can legitimately mint. It arrived
indistinguishable from the owner typing directly.

**The general defect (named by the peer):** when session A relays the owner's
instruction into session B, B receives it signed and attributed to the owner —
but A may have paraphrased, expanded, or bundled its own additions. The token
authenticates the *daemon*; the attribution names the *owner*; the content
passed through an **unlogged rewriting step** in between. **B cannot distinguish
"the owner said X" from "A's rendering of X" from "X plus what A wanted" — ever,
from inside B, by design.**

**Fix, and it is not a new subsystem.** `cli.py:202-219` already does this for
the *restart* path: takes `--from`, else auto-detects the initiator from cwd (a
session running in `~/transcripts/imessage/<chat>` identifies itself), and
stamps `initiator_chat_id` — comment: "so sessions know who triggered the
restart". `cmd_inject_prompt` (cli.py:697) captures none of it, and the
`session.injected` event has no such field.

So restarts must say who asked and **admin-tier instructions need not**. Apply
the same eight lines to the more dangerous command: an override should arrive as
**"session X relaying <owner>: \<verbatim\>"**, so the relayed part and the
relayer's additions can be held to different standards.

**Status:** daemon code, needs owner approval and a restart. Unstarted.

**Framing for whoever implements it (peer's, and it matters):** the restart
marker proves the idea was *already had*. Someone thought about provenance,
implemented it correctly, and scoped it to the **recoverable** command. So this
is not "nobody considered it" — it is **"it was considered and scoped wrong."**
That makes the change uncontroversial: there is no design argument to have, only
an existing decision to apply consistently to the dangerous case.

**Corroboration:** 21,318 `session.injected` events on the peer box + 1,027 here
= **22,345 injections across two machines, zero carrying an initiator field.**
The peer's key set is a strict superset of mine (`display_name`, `emoji`,
`inject_ms`), so this cannot be a schema trimmed on one side — the box with
*more* fields is missing the same one. It was never designed.

### Appendix: 2026-08-28 — My Watchdog Fix Was Scoped to the Instances I'd Seen

The blocked-session watchdog fired again at 10:11. **False positive**, and it
proves my recommended fix was under-scoped.

- Commitment marker: **absent** (`no outstanding commitment`), so this is the
  heuristic path.
- The "commitment" it quoted: `"ornia is wrong, and I'll check pending items
  against those dates befor"` — a **mid-word fragment of my own summary prose**,
  not a promise to anyone.
- Pattern that fired: `\bi['’]?ll\s+(?:get|do|check|...)\b`, on *"I'll check"*.

**Why this matters.** On 2026-08-26 I found all three historical heuristic false
positives came from a single regex, `\bon it\b`, and recommended tightening that
one. **This fires from a different pattern in the same list.** My fix would not
have prevented it.

That is precisely the error I criticised in the attachment-schema patch hours
earlier: someone hit the `size` KeyError, fixed the one key that bit them, and
left `name`/`path`/`mime_type`. **I did the same thing** — generalised a fix from
the sample I happened to observe, on a list of twelve patterns, having checked
three.

**The real defect is not any individual regex.** The heuristic scans
`last_assistant_text` — the agent's own narration. An agent that routinely
writes "I'll check X", "I'll look at Y", "working on it" in ordinary summaries
will trip a promise-detector indefinitely, because **the corpus being scanned is
its own prose about intended actions.** Tightening patterns one at a time is
chasing samples; the surface is unbounded by construction.

**Revised recommendation** (supersedes "tighten the on-it regex" on the open
list): the heuristic should not key on assistant prose at all. If a marker-free
backstop is wanted, key it on something observable — e.g. an unanswered inbound
message with no outbound reply — which is a fact about the conversation rather
than a guess about intent.

**Action taken:** none. No marker to clear, nothing blocked, nothing to
escalate. Recorded and stayed quiet.

---

## 2026-09-07: Deferred chat.db inbound writes (App Nap)

**Severity:** Medium (user-visible latency, no data loss)
**Duration:** 18m40s end-to-end for one message; recurring for at least a week
**Impact:** Eric messaged from a Lawson at 21:46:07 EDT and the reply looked like
it took 15+ minutes. The daemon and the session were both innocent.

### Symptom

A huge apparent reply latency with **no daemon fault anywhere in the logs**. The
message simply is not in `chat.db` yet, so nothing downstream can see it.

Measured split for the 2026-09-07 case:

| Stage | Time | Share |
|---|---|---|
| Send → row lands in `chat.db` | 18m23s | 98.5% |
| Row → daemon injection | 70ms | ~0% |
| Injection → my reply sent | 16s | 1.5% |

Proof it was not the daemon or the session: his follow-up at 22:07:07 was picked
up in the **same second** and answered in 10.6s.

### Root Cause

macOS deferred the **inbound** `chat.db` write because Messages.app was
deep-backgrounded and App Nap suspended `IMDPersistenceAgent`. The database was
healthy the entire time — only the inbound write path stalled.

### Discriminator — how to identify this in 60 seconds

1. **Outbound rows write instantly while inbound rows stall.** This is the key
   test. If your own sent messages are landing in `chat.db` immediately, the DB,
   the disk and the file handles are all fine, and the problem is upstream of
   the DB on the inbound path only.
2. **The poller shows `found=0` across thousands of consecutive polls.** Here it
   ran ~11,000 times at 100ms with no missed polls, the same query, zero rows.
   The row genuinely was not in the database.
3. **The daemon's own STALENESS metric spikes.** It logged `1103322ms`, and
   `chatdb_stale` reached 66604s — 18.5 hours with no inbound row at all.

If all three hold, it is App Nap, not the daemon.

### Fix Applied

Messages' bundle id on macOS is **`com.apple.MobileSMS`**, *not*
`com.apple.iChat` — writing to the latter does nothing.

```bash
defaults write com.apple.MobileSMS NSAppSleepDisabled -bool YES
defaults write com.apple.messageshelper.MessageController SOAppNapDisabled -bool YES
# the setting is read at launch — quit and relaunch Messages.app
```

**Persisted so a reinstall can't lose it** (the hand-applied fix was machine
state, not code): `bin/setup-macos-defaults` applies/verifies both keys
idempotently (`--check` exits non-zero if either is unset; `--restart-messages`
does the required quit+relaunch), it is a step in README's Setup block, and the
daemon re-checks it hourly as the `imessage_app_nap` dependency-health check
(`assistant/dependency_checks.py`) — recovery re-writes the keys and logs a
WARNING that Messages.app needs a relaunch. macOS updates can reset defaults, so
install-time-only would not have been enough.

Both keys verified as `1` after the write. **Not yet proven effective** — the
failure only manifests after Messages idles for hours, so a verification
reminder is armed for 2026-09-08 08:00 EDT.

### The escalated tickle does NOT reliably flush the write

The existing mitigation fired at 21:45:56 and 22:00:57, both reporting
`launchctl kickstart imagent OK` and `open -a Messages OK`. **Neither flushed
the pending write.** Do not treat a successful tickle as evidence the backlog
cleared.

It is also chronically firing — `tickle_escalated` has fired 72–88 times per day
for the past week, so its escalation condition is permanently true and it no
longer functions as an escalation.

**Retired 2026-10-26.** The escalated tickle (`_imessage_tickle_escalated`) is
removed. Its `launchctl kickstart` was always blocked by SIP (rc=150), and in 8
days of bus data only 2 of 670 firings were followed by a `message.received`
within 30s, which is chance level. App Nap is now covered by the
`imessage_app_nap` dependency check plus the light 30s tickle. The
`imessage.tickle_escalated` event is no longer emitted, so the query below only
returns historical rows.

### Verification Queries

Staleness spikes over 60s in the daemon log:

```bash
grep -E "\| STALENESS \|" ~/dispatch/logs/launchd.log | awk -F'staleness=' '{split($2,a,"ms"); if (a[1]+0 > 60000) print}'
```

Daily escalated-tickle counts (**`bus.db` timestamps are MILLISECONDS** — hence
the `/1000`):

```bash
sqlite3 "file:$HOME/dispatch/state/bus.db?mode=ro" "SELECT date(timestamp/1000,'unixepoch','localtime') d, count(*) FROM records WHERE type='imessage.tickle_escalated' GROUP BY d ORDER BY d DESC;"
```

### Prior Occurrences

The 18m23s deferral is the second-worst on record. Other recent spikes: 25m00s
(09-03), then 14m35s, 14m32s, 13m31s and 11m19s (all 09-06).

### Lessons

1. **"Slow reply" is not the same as "slow assistant."** Measure the three
   timestamps — chat.db send time, daemon injection time, reply send time —
   before assuming the latency belongs to anything you control.
2. **A mitigation that reports OK is not a mitigation that worked.** The tickle
   returned success twice while the write stayed pending.
3. **An escalation that fires 80x/day is not an escalation.** It needs a
   threshold that is false most of the time, or it carries no signal.

---

## 2026-09-21: Assistant Unresponsiveness — Two Root Causes, Neither Was The Network

**Severity:** High (user-visible; the top complaint for weeks)
**Impact:** 6.5h of dropped replies + p90 inbound detection lag of 431s

### Ground truth
Reply latency (inbound iMessage -> next outbound, chat.db), median vs p90:
09-10 32s/564s · 09-15 27s/488s · 09-17 40s/1157s · 09-18 694s/17221s ·
09-19 86s/2829s · 09-21 29s/901s
The p90 tail of 8-20 MINUTES predates the 9/18 network incident. Median was
always fine. **The tail was the complaint, and it was never the network.**

### Cause 1 — errored turns send nothing, and nothing retries
Counted over the whole bus history, verified independently of the agent that
first proposed it:
    errored turns: 49 sent no outbound, 9 replied
    clean   turns: 17 sent no outbound, 88 replied
~84% of errored turns end in silence; clean turns are the mirror image. Total
23,339s (6.5h) of dead air. The silence ends only when the human re-pings.
NOTE: a single spot-check APPEARED to refute this (it hit one of the 9 that did
reply). Checking all 163 turns is what settled it — n=1 nearly killed a correct
finding.

### Cause 2 — macOS defers chat.db writes, and the mitigation never worked
Inbound detection lag: median 0.2s, **p90 431s, max 1244s**, bimodal (79.7%
<1s, 19.5% >60s). On quiet days 92-98% of the worst replies were purely this.
App Nap parks Messages.app/IMDPersistenceAgent after idle.
`_imessage_tickle_escalated()` runs
    launchctl kickstart -k gui/<uid>/com.apple.imagent
which returns **150 "Operation not permitted while System Integrity Protection
is engaged"** — i.e. it cannot work on any Mac with SIP on (the default). The
code awaited `proc.wait()` and then logged "OK" **without ever reading
`returncode`**, 549 times, while imagent sat at 52 days uptime.
Second defect: the escalation gate required `messages_idle > 3600s`, so it could
never fire during a live conversation.

### Fixes
1. `defaults write com.apple.Messages NSAppSleepDisabled -bool YES` (+ restart
   Messages). The actual mitigation, never previously set.
2. `sdk_session._notify_if_reply_dropped()` — on an errored turn, check chat.db
   for an outbound inside the turn window; if none, SMS the user. Debounced
   600s (9/18 had a 78% error rate; must not become an SMS storm). chat.db is
   ground truth because the agent replies via the send-sms CLI, so the session
   object never sees the send.
3. kickstart now branches on returncode and names the SIP block.
4. `MESSAGES_IDLE_THRESHOLD` 3600 -> 600.
Tests: `tests/unit/test_dropped_reply_notice.py` (4). Suite 1463 pass, 10
pre-existing failures unchanged.

### Lessons
- **Median hid the problem.** ~30s median looked healthy for weeks; the p90 was
  8-20 minutes. Always look at the tail for a latency complaint.
- **A success log that never checks returncode is worse than no log** — it
  actively prevented discovery for 549 occurrences.
- Three network theories (bufferbloat, channel congestion, Cloudflare) were all
  wrong AND all irrelevant: the real causes were a dropped reply path and an
  OS-level write deferral. Chasing the most recent incident (9/18) obscured the
  chronic problem visible in the same data since 9/10.

### Deployment note (2026-09-21 11:11)
`claude-assistant restart-sessions` does NOT reload daemon code — it restarts the
SDK child sessions only. `manager.py` / `sdk_session.py` live in the daemon
process, so after editing them the PID must actually change. First attempt left
PID 93435 running for 12h with the old code while appearing to have "restarted".
Correct sequence (detached, because stopping the daemon kills the session that
issued the command):

    nohup bash -c 'claude-assistant stop; sleep 4; claude-assistant start' &

Verify by comparing `ps -o lstart= -p <newpid>` against the file mtime of the
edited module — daemon start must be LATER than the edit. Confirmed here:
manager.py 11:02:53, daemon 52791 started 11:11:50.

### CORRECTION 2026-09-21 14:20 — Cause 2 above is WRONG. Read this before acting on it.

"App Nap parks Messages.app" is **not** the cause, and the
`defaults write com.apple.Messages NSAppSleepDisabled` fix targets a process
that does not write chat.db. `lsof` shows **IMDPersistenceAgent is the sole
writer**; Messages.app never touches it.

**This is the second time this wrong answer was reached.** It was derived
2026-09-07, retracted with evidence 2026-09-08 (see the group thread with Sven
Thorat / the dispatch fork author), and then re-derived from scratch and
re-applied on 2026-09-21 by a session that never read the retraction. The
`com.apple.MobileSMS` variant was applied on 09-07, verified reading `1`, and
staleness spikes continued all night — the fix was already empirically falsified
two weeks before it was re-applied.

**Actual cause: APNs, not local persistence.** apsd's socket to
push.apple.com:5223 is RST by the wifi/NAT path; apsd does not detect it because
the negotiated keepalive is 1560s, so Apple queues inbound for up to ~26 min.
Evidence: 8-of-8 correlation between STALENESS spikes and imagent "APS
Connection status did change NO->YES"; and the ceiling — of 383 STALENESS
warnings the maximum is **24m59s** against a **1560s** keepalive. A local write
deferral has no reason to stop at exactly the keepalive interval.

**The p90 "detection lag" of 431s in the entry above is overstated.**
`message.date` is the SENDER's clock, so that metric measures Apple-side queue
time, not local detection lag. Use rowid insertion order to measure local lag.

**Fix applied (UNVERIFIED):** `net.inet.tcp.always_keepalive=1` and
`net.inet.tcp.keepidle=180000` (was 7200000) in /etc/sysctl.conf — recommended
2026-09-08 but never actually applied until now. Cannot currently be validated:
imagent and apsd emit zero unified-log lines on this box, so the APS-reconnect
correlation that was the original evidence is unobservable.

**Cause 1 (errored turns sending nothing) is unaffected** — measured
independently from bus/chat.db turn outcomes.

### The real lesson
Prior findings lived in a group iMessage thread that no later session read. A
conclusion that is not written into the repo does not exist. Search
POSTMORTEMS.md **and** past conversations for a symptom before investigating it
— this cost a full re-derivation, a wrong fix applied to production, and a
"root cause found" claim to the user that had to be retracted.

### 2026-09-21 (later): the api_uplink probe became the incident
After the uplink work above, the probe itself paged the admin **16 times in two
days**, each a 686-char SMS. He said: "You have got to fix that."

**Two defects, both mine:**
1. **Paged on a single failure** for a fault that is intermittent by nature. The
   `net.inet.tcp.autosndbufmax=393216` cap took 2MB uploads 0/10 → 10/10, but a
   residual ~1-in-10 blip remains and self-clears in seconds.
2. **I had stuffed the day's findings into the alert string** ("re-test a
   NON-Cloudflare host", "Do NOT advise rebooting the router", measured speeds).
   Correct guidance, wrong audience — that text is delivered to a human's phone,
   not to an engineer reading a log.

**Fix:** retry twice (3s apart) before escalating; only 3 consecutive failures
page. Message cut 686 → 218 chars, carrying only what the human can act on. The
diagnostic narrative moved into code comments next to the constants.

**Tests:** `TestIntermittentRetry` (4 cases) — single blip and double blip must
NOT page; three consecutive failures MUST still page (the guard is a delay, not a
mute); a clean pass must not retry at all. Suite 1467 pass, 10 pre-existing fails
unchanged.

**Two existing tests had to be rewritten**, and they deserved care: they
deliberately asserted the long text to prevent a *previous* wrong-advice bug
(sending the operator at the ISP/router). I kept that lesson as
`assert "thernet" not in detail` / `assert "reboot" not in detail.lower()` /
`assert len(detail) < 400` rather than deleting the intent along with the string.

**Lesson:** an alert string is a user interface. Debugging context belongs in
comments and diagnostics; the notification should contain only the action. Also:
"ethernet is not an option here" is a deployment fact the code must encode —
it was re-suggested twice after the user ruled it out.

---

## 2026-09-22: Two Silent Failures on One Package — usps-watch

**Severity:** Moderate (user-visible; owner missed both notifications for a
package he was waiting on, while the log reported success)

### What happened
`bin/usps-watch` watched one hardcoded USPS number ("Emily's package"). It
failed twice, in two different ways, and reported health both times:

1. **2026-09-21 09:15 — unverified send.** Log line: `1-day notice sent`.
   There is **no corresponding row in chat.db** for that time. The script called
   `send-sms` and never checked the exit code. (This predates the 2026-09-21 fix
   that made send-sms verify delivery and exit non-zero — but the script would
   still have ignored the new exit code.)
2. **2026-09-22 09:15 — delivered-state blindness.** Log line:
   `could not parse date; block=[/]`. Root cause: the script grepped the USPS
   page for `Expected Delivery by`, but **once a package is delivered USPS drops
   that block entirely**. So it fell through the delivery check (which required
   BOTH a status line AND the exact prose "your item was delivered"), hit the
   date parse, failed, and exited. A delivered package logs that error forever
   and never notifies.

Owner learned of the delivery only by asking. `track 9534616388586260386892`
reported `Delivered` immediately — the data was always available.

### Fix — bin/package-watch (new), usps-watch retired
- Built on `track --json` (fields: tracking/carrier/status/eta) instead of
  re-scraping. `status == "Delivered"` is authoritative; no prose grepping.
- Watches an arbitrary list from `state/package-watch.json`, all 3 carriers.
- Notifies once each on: first ETA known · ETA <=1 day · delivered.
- **Treats a non-zero send-sms exit as failure and does NOT record state**, so a
  failed notification retries next run instead of being lost. This is the direct
  fix for failure #1 — recording state on an unverified send is what destroyed
  the notification permanently.
- LaunchAgent runs 4x/day (08/12/16/20:15). The old 09:15-only schedule meant an
  ETA appearing at 09:30 went unreported for 24h.
- `com.dispatch.usps-watch` unloaded to avoid two schedulers double-notifying.

### Lessons
- **A watcher that reports its own success without verifying delivery is worse
  than no watcher** — it converts a missing notification into a silent one. Third
  occurrence of this exact pattern in three days (send-sms "SENT", kickstart
  "OK", watcher "notice sent"). Any log line asserting success must be derived
  from a checked return value, never from reaching that line of code.
- **Terminal states change page structure.** Parsers keyed on a field that only
  exists in the non-terminal state ("Expected Delivery by") break exactly when
  the event you care about happens.

## 2026-09-23: package-watch failed silently twice in 24h — both while looking healthy

**Severity:** Low impact, high signal. Third instance this week of the same bug class.

### Failure 1 — exit 127, every scheduled run, no output
The LaunchAgent invoked `bin/package-watch` directly. Its shebang is
`#!/usr/bin/env -S uv run --script`, and `uv` lives in `/opt/homebrew/bin`, which
is NOT on launchd's minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Every run
died instantly with `env: uv: No such file or directory`. Two scheduled runs lost
(20:15, 08:15). `launchctl list` showed exit **127**.
**Why it hid:** I tested the script by hand, where my shell PATH has uv. I never
tested it the way launchd runs it. The predecessor `usps-watch` worked only
because its plist called `/bin/bash` explicitly.
**Fix:** `EnvironmentVariables → PATH` in the plist including /opt/homebrew/bin.

### Failure 2 — ETA parser could never fire the 1-day alert
`datetime.strptime(eta, "%Y-%m-%d")` only. FedEx actually returns **"9/25/26"**.
Every parse raised into a bare `except: continue`, so the "arrives tomorrow"
notification was unreachable code in practice.
**Why it hid — this is the important part:** the FIRST-ETA alert does not parse
the date, so it worked perfectly. The only dead path was the one that matters on
delivery day. The watcher looked healthy from every angle except the one nobody
checks until it's too late.
**Fix:** `_parse_eta()` trying 7 formats, returning None rather than raising.

### The pattern, now three-for-three this week
1. `send-sms` printed SENT on hard failures (error=22) — 2026-09-21
2. `_imessage_tickle_escalated` logged "kickstart imagent OK" without reading
   returncode — 549 consecutive false successes — 2026-09-21
3. Both failures above — 2026-09-23
Every one: **the component reported success or said nothing, while doing
nothing.** None produced an error anyone would see.

### What I did differently this time
Wrote `tests/unit/test_package_watch.py` (15 tests) and then **verified the tests
actually catch the bugs** by reintroducing the ISO-only parser — 6 failed, as they
should; restored, 15 passed. A test that has never seen the bug fail is a guess.
Includes `TestShebangIsLaunchdSafe`, which asserts that if the shebang needs `uv`
then the plist must supply a PATH containing it — i.e. failure 1 cannot recur
silently.

### Lesson
**Hand-testing validates the happy path in the developer's environment.** Both
failures were invisible to hand-testing by construction: one depended on the
execution environment (launchd PATH), the other on a branch that only runs on
delivery day. When a component's whole job is to act later, in a different
environment, testing it now in this one proves almost nothing.

### 2026-09-23: the no-reply watchdog could not see most replies
Eric: "Your messages are too long." I replied twice (12:49:33, 12:50:33, both
error=0). Thirteen minutes later the watchdog injected "you have not sent ANY
reply since".

**Cause:** `_is_send_command()` matched only the FIRST TOKEN of the Bash command
against the send-script patterns. The documented way to reply is
`cd ~/transcripts/<backend>/<id> && .../reply "..."` — because the reply CLI
infers chat_id from cwd — so the first token is `cd` and essentially every real
reply was invisible. `last_send_at` never advanced, so the session looked silent.

**Why it matters:** a false "you never replied" nudge invites re-sending a
message the user already received. Eric had complained about duplicate texts
earlier the SAME DAY (the package watcher repeated ETAs I'd given by hand). The
watchdog was manufacturing the exact failure it sits next to.

**Fix:** split the command on `&&`, `||`, `;`, `|` and newlines; test the first
token of each segment. Keeps `echo ".../send-sms"` a non-match (arguments are
never checked), so it cannot silently suppress a real nudge.

**Tests:** `TestSendCommandInCompound` (5 cases) — the exact `cd && reply` form,
`;`-chained, a pipeline, plus guards that a bare mention in an argument does NOT
count and that ordinary commands still don't. Verified they fail against the old
first-token logic before fixing. Suite 1491 pass, 10 pre-existing unchanged.

**Lesson:** a detector that reads only the first token of a shell command will
miss the documented invocation pattern of its own system. Test detectors against
the form the docs actually tell you to type, not the simplified form.

---

## 2026-09-24 — SENT is not delivered; phantom sends; bots relabelled unknown

Four findings, each cross-checked against a second machine (Sven's box).
Where the two boxes disagreed, that is recorded rather than averaged away.

### 1. A dead room: 17 messages, 5 weeks, zero arrivals, every one "SENT"

Group `329b03fdfc…` received 17 outbound messages aug 19 → sep 22. All
returned SENT with **error=0**. The recipient confirmed from his own machine
that none arrived. Nothing caught it because:

| send type      | delivery confirmed | `is_delivered` usable? |
|----------------|--------------------|------------------------|
| 1:1 iMessage   | 2573 / 2573        | yes — perfect signal   |
| group iMessage | 0 / 173            | no — **never set**     |
| group SMS      | 0 / 17             | no — **never set**     |

Reproduced independently: 2332/2332 vs **0/2261**. Structural in Messages.

**`SENT_GROUP` is therefore unverifiable by construction.** `send-sms` already
has `verify_delivery()` reading chat.db `error`, and it is working correctly —
these rows had error=0. No per-send check can close this.

The only available defense is a periodic *config* smell, now shipped:
`~/.claude/skills/sms-assistant/scripts/check-dead-rooms`. Flags groups that
are monologue-only or SMS-routed with >1 member. Catches `329b03fdfc…`, no
false positives on the 4 healthy groups.

**Contributing cause: one person, two handles.** The dead room addressed Sven
at `+15555550199` (SMS, 0 inbound ever). He participates as
`friend@example.com` in a different iMessage group.

**Unfixed:** 4 of 6 send paths — `send_image_individual`, `send_image_group`,
`send_file_individual`, `send_file_group` — never call `verify_delivery` at
all, so an image to an unroutable handle still prints SENT_IMAGE on error=22.

### 2. Phantom sends: heredoc bodies parsed as shell — FIXED

`_is_send_command` splits on `\n`, so every heredoc body line became its own
shell segment. Any body line starting with a send-script path registered a
send — which is exactly what we write when documenting or testing the send
CLIs. **Confirmed on the live bus: the heredoc in which the detector was
tested emitted a real `message.sent`.** The test tripped the bug it tested.

Direction of harm is quiet: a phantom advances `last_send_at`, which
**suppresses** the unanswered-inbound watchdog. Real messages can go
unanswered with no nudge.

Fixed by `_strip_heredoc_bodies()` (Sven's prescription: strip heredocs, keep
bare `|` out of the split). 5 regression tests added; 54/54 pass; every
pre-existing contract held including `test_pipe_to_send`.

**Requires a daemon restart to take effect** — the running daemon (started
sep 18–20) holds the old function in memory.

### 3. Finding #1 from aug 27 is still live: bots relabelled `unknown`

`manager.py:5435` gates on `("admin","partner","family","favorite")`. **`bots`
is absent**, so a bots-tier sender falls to the `elif existing_session` branch
which hardcodes `sender_tier="unknown"` and `sender_name=phone`. The correct
`lookup_identifier()` result (`Sven Thorat` / `bots`) is computed and then
discarded.

Consequence: a bots collaborator arrives tagged with the one tier the rules
say to ignore ("unknown: ignored, no response, no engagement"). It works only
by accident — `existing_session` being true. In a *new* group, a bots sender
matches no branch at all.

Confirmed on both machines (`5435` here, `5404` on Sven's). Three sibling
tuples — `5494`, `5565`, `manager.py:634` — need individual judgement, not a
blind copy: **5565 is a deny check, so adding bots there restricts.**

**Status: awaiting Eric's go for 5435 only.**

### 4. Method: the integer/TEXT strftime comparison, again

`strftime()` returns TEXT; `m.date/1000000000 + 978307200` is INTEGER. In
SQLite an INTEGER is always less than TEXT, so the comparison is **always
false** — zero rows, no error, indistinguishable from "nothing found". This
made me tell the group there was no cross-chat context when the admin had
just explained it in another thread. Same bug Sven hit the same hour.

**Rule: pair every negative result with a positive control.** Both of us now
quote controls alongside negatives; that habit caught this within minutes.

### 5. Retraction: I called a mitigation a default (same day)

Told the group Sven's uplink control was invalid because "my box reads
`autosndbufmax: 393216`, not the 4194304 you tested at." **Backwards.**
393216 is the mitigation *we applied on 2026-09-21*, persisted in
`/etc/sysctl.conf` with the measurement in the comment: "0/10 uploads passed
at 4194304, 9/10 at 393216." 4194304 was the failing state, so his test was
correctly scoped; BDP was its only real limitation, exactly as he said.

I read a live sysctl value and inferred "default" without checking whether
anything had set it. One `cat /etc/sysctl.conf` refutes it. The entry three
paragraphs above this one already says: *"A conclusion that is not written
into the repo does not exist. Search POSTMORTEMS.md before investigating."*
It **was** written down, by me, and I still asserted over it.

**Rule: before calling a config value a default, find what sets it.** A live
value tells you the state, never the provenance.

Follow-on measurement (3× 8MB to speed.cloudflare.com): **4.61–4.79 MB/s** at
**16.7 ms** RTT → steady-state in-flight ~78 KB, about 6× *below* the ~480 KB
knee. Reaching the knee at this RTT needs ~29 MB/s. So >480 KB unacked cannot
be steady state here; it must be a burst — the send buffer draining faster
than ACKs return. Next check: pcap of a failing run at 4MB (netstat -s -p tcp
reports 0 packets sent on macOS 26.2 — dead on both machines, so it can't
answer whether retransmits occur).

### 6. Dropped-reply notice never fires for group chats

`sdk_session.py:1265` `_notify_if_reply_dropped` tells the user when an errored
turn sent them nothing. Line 1288 guards `if not str(self.chat_id).startswith("+")`,
so **group sessions are excluded entirely** — a dead turn in a group is silent.

Checked for the inverse bug too: the "did we reply" query joins
`handle h ON m.handle_id = h.ROWID WHERE h.id = ?` against `self.chat_id`. A
hex group id can never match a handle id, so without that guard it would fire
falsely on *every* group turn. The guard returns first, so this is a coverage
gap, not a false-alarm bug.

Sven's framing is the right one regardless: **notifying is not retrying.** On
both machines an internal error ends the turn and the human must re-send. The
notice is an apology, not a fix.

### 7. "Not an ISP or router fault: verified" — an unproven cause, shipped to a human

`dependency_checks.py` `_UPLINK_ESCALATION_FIX` asserted the fault was **not**
the network, "verified", in the SMS delivered to the admin during an incident.
It was never verified. The basis was `netstat -s -p tcp` showing no
retransmissions — and that counter is dead on macOS 26.2, reporting 0 packets
sent on a machine that is actively networking (confirmed on a second Mac, so
it is the OS). **A sender holding unacked data must retransmit after RTO**, so
"zero retransmits" could never have been a real observation.

Worse than being wrong in a doc: it steers the admin away from the path, which
is the current leading hypothesis, at the moment he is looking at the problem.

Reworded to "No router or ISP action needed; root cause not yet isolated." The
operational lesson (never dispatch the human to the router — that wasted his
time and 16 SMS) is preserved; the causal claim is gone. The test that pinned
the old wording now asserts `"verified" not in detail`.

**Rule: an alert may carry what to DO. It must not carry a cause we cannot
show.** Prescription and diagnosis have different evidence bars.

### 8. Dropped-reply notice: guard removed, query fixed, Signal gap closed

Finding #6 said groups were excluded from the dropped-reply notice. Sven's box
has no such guard and its notices went to three separate group chats on sep 17.
Groups are where a dead turn goes unnoticed longest — everyone assumes someone
else saw it. Guard removed.

**It could not simply be deleted.** The lookup joined `handle h ON m.handle_id
WHERE h.id = ?` against `self.chat_id`; a hex group id can never equal a handle
id, so every group turn returned no row and would have reported "nothing was
sent" *immediately after a real reply*. Verified against live chat.db: for a
group replied to 4 minutes earlier, the chat join finds the row and the handle
join does not. Groups now use the chat join — the shape `verify_delivery`
already uses. Same 1:1-vs-group identifier confusion as the dead room (#1);
that is now three bugs from one root confusion.

**Latent bug found alongside:** a Signal 1:1 `chat_id` also starts with `"+"`,
so it passed the old guard, queried chat.db (which holds no Signal messages),
found nothing, and would have fired a false "my last turn failed" notice
**over iMessage to a Signal contact**. No Signal sessions exist today, so it
never fired. Added a `source` guard.

**Still open on both machines: notifying is not retrying.** An internal error
ends the turn and the human must re-send. Also, both implementations fire only
on `is_error` — a turn that ends *cleanly* having sent nothing is invisible to
both. That is the real remaining gap.

**Blocked:** splitting path-eats-the-burst from local-stack-stall needs a pcap
of a failing 4MB upload. `/dev/bpf0` is root-only and sudo requires a password.
Everything else in this investigation was doable unprivileged.

### 9. [RETRACTED IN #11] The dropped-reply notifier has never run — and neither has 5277 lines of other work

Claimed to Sven that the notifier "exists on my box" and that I was not behind
him. Both wrong in the way that matters:

```
git show HEAD:assistant/sdk_session.py | grep -c _notify_if_reply_dropped  ->  0
grep -c DROPPED_REPLY_NOTICE logs/manager.log                              ->  0
      (log spans 2026-04-28 -> 2026-09-24, unrotated)
```

It is an **uncommitted working-tree edit that has never executed**. The running
daemon started sep 18-20 and does not contain it. Sven's equivalent is in
production and has sent real notices to three group chats. He has a notifier;
this box has a file.

**Systemic, not local:** `git status` shows **43 files / 5277 insertions**
uncommitted on `feat/sms-length-guard`, and the LaunchAgent runs
`exec "$DISPATCH_DIR/.venv/bin/python" -m assistant.manager` straight from the
working tree — no checkout, no pinned ref. So:

1. Work gets written, declared shipped, and never deployed or committed.
2. "Restart the daemon" is **not** a routine action. It deploys the entire
   uncommitted tree in one step, including weeks of never-executed code.

I asked the admin for a restart before checking either fact. **Rule: before
asking for a deploy, check what the deploy would actually ship.**

### 10. The silence this was supposed to catch — measured, and it is the top item here

Sven checked his tail and found zero victims: three long gaps since sep 15, all
"thanks"-type messages correctly answered with a tapback inside a minute. Same
check on this box, reading every message rather than trusting the aggregate:

| when | gap | what the admin sent |
|------|-----|---------------------|
| 09-18 10:11 | 278m | "Think I'll want some kind of speaker…" |
| 09-18 12:09 | 160m | **"You there?"** |
| 09-18 18:05 | 287m | **"It's been 3 hours"** |
| 09-18 22:00 | 52m  | **"are you okay?"** |
| 09-19 12:39 | 269m | "So how am I supposed to mount these" |
| 09-19 18:30 | 283m | "And this is all just for the sim rig…" |

**18 gaps >20 min across 173 inbound messages since sep 15**, three of them the
admin asking whether the system was alive. This is the concrete content of the
"why are you unresponsive" complaint.

**Caveat, stated because it limits the claim:** every one of these predates any
running notifier, so they are not cases where it failed — they are cases where
nothing existed. It cannot yet be claimed the notifier would have prevented
them. Sven's box is the evidence that the mechanism works once deployed.

**Same input, opposite ranking on two machines.** "Rank it accordingly" is
correct advice that yields "lowest priority" there and "highest" here. Cross-box
agreement on a *fact* does not transfer a *priority* — check the tail locally.

### 11. Retraction of #9 — "not in HEAD" does not mean "not running"

Finding #9 claimed the notifier had never executed and that a restart would
deploy 5277 uncommitted lines. **Both wrong.** Sven ran the check I skipped:
compare daemon start time against file mtimes.

```
daemon start (state/daemon.pid): 2026-09-23 13:11:21
assistant/*.py modified AFTER start:   2   (dependency_checks.py, sdk_session.py)
control, modified BEFORE:             25
```

Both are files edited *today, by me*. The uncommitted tree was already on disk
when the daemon started, so **it is already running**. The notifier is
deployed. A restart ships today's tested changes and nothing else.

Two compounding errors produced #9:

1. **Misread `ps`.** Reported the daemon as starting sep 18–20 from unrelated
   PIDs, never reading `state/daemon.pid`. The real start is sep 23 13:11.
2. **Inferred "not in HEAD" ⇒ "not running"** on a daemon that runs from an
   unpinned working tree — where that inference is precisely backwards.

**Near-miss on the vacuous negative.** The next claim would have been "zero
errored turns since start, so it never had an opportunity":

```
result events since daemon start:  37, errored: 0
all-time:                          87, errored: 0   <- control
```

Zero errored turns *all-time* is not credible beside this file's own "49
errored turns sent no outbound message." `is_error` on result events is
**unpopulated, not clean.** The all-time control is the only reason that
didn't ship as a finding. Whether the notifier ever had an opportunity to fire
is **undetermined** — do not claim otherwise without a working error signal.

**A risk already taken, unnoticed.** Earlier this session, to test whether two
failures were pre-existing, this session ran `git stash push --
assistant/sdk_session.py`, pytest, then `git stash pop`. For ~25 seconds the
running daemon's notifier existed only in a stash. A restart in that window —
which the watchdog can trigger — would have silently rolled production back.
That is exactly Sven's reset/stash scenario, walked into an hour before he
named it.

**Rule: on a machine whose daemon runs from an unpinned working tree, `git
stash` and `git reset` are production operations.** Check the daemon's start
time against file mtimes before reasoning about what is deployed — and never
stash a file the running daemon loads.

**What survives:** the code is still uncommitted and still not in HEAD. Sven's
framing is the right one — his box is unversioned code, this one is code ahead
of its commit; same root, same fix: **commit, then pin the daemon to a ref.**

### 12. `sdk_events` is a rolling window — "all-time" needs the archive

Reported "0 errored results all-time (87 events)" as a control, and used it to
argue `is_error` was unpopulated. Sven built on it and concluded the notifier's
trigger had never been true. **Both wrong.**

```
sdk_events           4394 rows   09-21 10:12 -> now    result=  88  errored=  0
sdk_events_archive  89031 rows   04-28 -> 09-21 10:11  result=1704  errored= 81
```

`sdk_events` is a rolling window with `sdk_events_archive` behind it. Querying
one table and calling it all-time is the same vacuous-negative class flagged
twice earlier the same day — this time inside the control itself.

**The contradiction was already in hand and went unchased:** 18
`session.crashed` events carry `consecutive_error_turns=3`, and that counter
only increments *inside* `if message.is_error:`. The flag therefore had to have
been true. When a control contradicts other evidence on the same box, the
control is the thing to doubt first.

**Rule: before trusting a count, confirm the table is not a window.** Check
row-count and MIN/MAX timestamp against the period being claimed.

### 13. What the record actually shows, and why the notifier is still untested

| day | result turns | errored |
|-----|--------------|---------|
| 09-17 | 25 | 0 |
| 09-18 | 52 | **38** |
| 09-19 | 33 | **17** |
| 09-20 | 27 | **3** |
| 09-21 → 09-24 | 97 | **0** |

Control that the column is still written: `sdk_events` `tool_result` has 1889
events with **41 errored**, so the post-09-21 zeros are real.

The notifier was written 09-21 and deployed 09-23 13:11. Errors stopped 09-21.
**It has been live across ~49 clean turns and has never met its condition.**
Untested in production — not because its trigger is broken (it fired 58 times
over three days) and not because it was undeployed (#11), but because the
failure it targets stopped happening two days before it shipped.

**[RETRACTED IN #14 — falsified by three independent checks]** ~~The unresponsiveness complaint and the uplink fault are one incident.~~
All 18 `session.crashed` events fall in 09-18 → 09-20 on the admin's 1:1, the
same window as "You there?", "It's been 3 hours", "are you okay?", and
`/etc/sysctl.conf` is dated 09-21 — the day errors stop.

Chain, stated as correlation and not proof:
**uplink stalls → turns error → 3 strikes → session crash → hours of silence.**

That raises the value of the pending pcap: it is not a networking curiosity, it
is the root of the admin's largest complaint. It also means the *residual*
~1-in-10 uplink blip is a live risk of the same cascade.

### 14. Retraction of the #13 synthesis — the fix postdates the fault it "fixed"

#13 proposed: uplink stalls -> turns error -> 3 strikes -> session crash ->
hours of silence, resting on "errors stop 09-21, sysctl.conf dated 09-21."
Sven proposed three fingerprint checks. **All three came back against it.**

**1. Duration — no 15s signature.** The stall's signature is a 15.0s RST.

```
errored  median 371.8s   min 339.8s   p90 1040.6s     n=58
clean    median  78.6s   min   0.0s   p90  319.6s     n=54
0 of 58 in a 13-20s band. 28 of 58 between 339-362s. Nothing below 339.8s.
```

A **hard floor near 340s** is a hang-and-timeout, not a stall-and-RST.

**2. Session length — opposite of predicted.** The large-request-body mechanism
predicts errors concentrate in long sessions.

```
errored  median num_turns = 1.0   mean  4.2
clean    median num_turns = 7.0   mean 11.4
```

Errored turns are the **short** ones.

**3. Chronology — decisive.**

```
last errored turn          2026-09-20 00:31:15
/etc/sysctl.conf mtime     2026-09-21 11:16:59    <- 34.8 h LATER
```

The mitigation was applied **a day and a half after the errors had already
stopped.** It cannot have caused the stop. The synthesis came from matching the
string "2026-09-21" in a code comment against a day bucket, never comparing the
file's mtime to the last error. **Rule: a date in a comment is not a timestamp.
Stat the file.**

**Confound correctly excluded:** archive/live cutover is 09-21 10:11 while the
last error is 09-20 00:31 — 33.7 h apart, with 37 clean results in the archive
after it. So "errors stopped" is real; only the explanation was wrong.

### 15. There is no error text on this box, and the log has a five-month hole

Sven's check 1 (what did the 58 errors actually say) **cannot be answered here.**

* All 81 archived errored `result` events have `payload = {}`.
  `send_sdk_event` records `is_error` but never the reason.
* `logs/manager.log` has **zero lines for 09-18**. It contains one stale line
  from 04-28 and then nothing until 09-21.

That invalidates an earlier control in #9: "0 `DROPPED_REPLY_NOTICE` across
04-28 -> today, unrotated" was `head`/`tail` of a file with a five-month hole.
**Rule: establishing a log's range means counting lines per day, not reading
its first and last line.**

**Open, and the most valuable thing left:** 58 turns with a hard floor at
339.8s, median 1 turn, confined to 09-18 -> 09-20, stopping on their own at
00:31 for an unknown reason. 3 strikes x ~350s matches the ~25 min crash
cadence, so the crash loop is downstream. **Cause is unknowable until `result`
events carry their error text** -- that instrumentation is the fix to make
before the next storm, and it is worth more than any theory built without it.

### 16. #14 was over-claimed — two of the three "falsifications" were invalid

Sven dismantled his own checks an hour after proposing them, and #14 rested on
them. Both objections are correct:

* **Check 2 (no 15s signature).** A turn is not one request. The CLI retries a
  failed API call (`CLAUDE_CODE_MAX_RETRIES`, default 10), so 11 attempts x ~15s
  = ~165s before backoff. A repeating 15s stall appears per-turn as a **floor**,
  which is exactly what the data shows. The 339.8s floor is *consistent* with
  the stall, not evidence against it.
* **Check 3 (errored turns are short).** `num_turns` is truncated by the error
  itself — any failure on the first call looks like a 1-turn session. On Sven's
  box 817 of 909 errored results have `num_turns=1`. It says nothing about body
  size.

**Only the chronology survives, and it is narrower than claimed.** One
falsification, not three.

### 17. When the A/B actually ran — and why the mechanism survives anyway

Sven asked the decisive question: `sysctl.conf` mtime records when the value was
*saved*, not when `sysctl -w` set it live. Answer found in this system's own
outbound SMS, not in a file timestamp:

```
[2026-09-21 10:19] -> Eric:
  "fixed. 2MB unthrottled x10: before 0/10 passed, after 9/10.
   capped TCP send buffer at 384KB (was 4MB). live now + persisted
   in /etc/sysctl.conf"
```

Live and persisted together, ~10:19 on 09-21 — **34 h after the last errored
turn (09-20 00:31)**. The cap did not stop the errors.

**But it was still 0/10 at 4MB at that moment**, which means the uplink fault
was *demonstrably live while 37+ turns ran clean*. On its own that kills the
mechanism.

**It doesn't, because something else changed first.** `scripts/uplink-guard.sh`
was created **09-18 22:53**, mid-storm, and its recovery prompt reads:

> "your predecessor session died because this Mac's uplink drops API requests
> with **large contexts (>~1.5MB body)**. Keep your context small: delegate web
> research to Task subagents returning compact summaries, never pull large pages
> into your own context."

A session during the storm independently reached the large-body mechanism and
shipped a **behavioural** mitigation. Errors then decay 38 -> 17 -> 3 -> 0
across 09-18/19/20.

**Reconciliation:** small contexts from 09-18 22:53 => no request crosses the
~480 KB knee => turns run clean, while the fault itself stays live until the cap
on 09-21. Fault present, symptom absent, because the payload stopped being large
enough to trigger it. **Sven's mechanism survives; only the "sysctl fixed it"
version was wrong.**

**Method note:** three times today a file mtime was used as an event timestamp
and was wrong or unprovable. The reliable record was the conversation — what was
said, when, with numbers in it. **Search chat.db before trusting a filesystem
timestamp for when something happened.**

**Still unresolved:** the crash cadence. `COOLDOWN_S=1500` (25 min) in
uplink-guard.sh matches the observed spacing, but the guard was created 09-18
22:53 while crashes begin 15:57, so it cannot explain the earlier gaps.
3 x 350s = 17.5 min vs observed 25-60 min. There is a wait between strikes not
yet located.

### 18. Sven's falsification tests for the body-size mechanism: run

Method: session jsonl for the admin 1:1, per-request `usage` (input +
cache_read + cache_creation), deduped by `message.id`, text bytes ≈ tokens × 4.

* **Denominator — survives.** Error *rate* per turn: 09-15..17 0%, 09-18 73%,
  09-19 52%, 09-20 11%, 09-21+ 0%. Not a weekend-traffic artifact.
* **Errored side — consistent, not decisive.** The storm begins inside a
  950,929-token session (≈3.8 MB text); its first errors (09-18 10:19, 12:28)
  are 343s/350s hangs. A context-window "prompt too long" would be instant, so
  it is not that. Later errors hit fresh sessions whose last *good* request was
  ~0.25 MB — but failed requests record no usage, so "last good" only
  lower-bounds the failing one.
* **Clean side — not killed.** 09-20 00:31 → 09-21 10:19 max ≈ 1.0 MB (257K
  tok), nothing ≥2 MB.
* **#17's rescue ("the guard shrank contexts") — not supported.** Post-guard
  clean hours (0.89–1.03 MB) were *larger* than errored hours (0.25–0.83 MB).
  Size does not separate errored from clean turns.
* **Images — unmeasurable this way.** Cumulative base64 per file gives 57 MB
  on 09-17, above the API's 32 MB request cap, so compaction drops old images
  and cumulative counting overstates. Text-only figures are the trustworthy ones.

**Open:** fault onset. 09-17 ran ~3.8 MB bodies clean all day; the same
session began hanging 09-18 10:19. Either the fault began 09-18 or body size is
not the trigger. Earliest uplink artifact on this box is 09-18 22:53.

**Deployed:** daemon restarted 2026-09-24 12:52:29, after the last
`sdk_session.py` edit (12:51:04). Live now: heredoc-aware send detector,
group-aware + Signal-guarded dropped-reply notice, error detail on `result`
events (`subtype`, `stop_reason`, `duration_api_ms`, result text), and the
uplink alert without the unverified causal claim. Still uncommitted.

### 19. The storm's error text was never lost — it's in the session jsonl

#15 said the error text was unrecoverable (bus payload `{}`, log hole). Wrong
place to look. The CLI writes a synthetic assistant entry per API failure
(`isApiErrorMessage: true`, `model: "<synthetic>"`, zero usage):

```
57 x "API Error: Connection dropped (ECONNRESET)"    09-18: 40  09-19: 17  09-20: 3
 1 x "Prompt is too long · automatic compaction failed: API Error: Connection dropped (ECONNRESET)"   09-18 14:45
```

Connection resets — not overload, not rate limits. The storm opens with the
~950K-token session attempting compaction (which uploads the entire context)
and being reset.

**Trap:** those synthetic entries carry `usage` with all zeros and a
`message.id`. A "last request before the error" query that doesn't skip them
picks the error itself and reports 0 bytes for everything. That happened here
first and returned 58 rows of zeros.

### 20. Failed request size — the fingerprint Sven asked for

Failed body ≈ last successful request (tokens × 4) + all content appended to
the same session after it (tool results, text, tool_use inputs).

```
60 failed requests      min 0.48 MB   median 0.77   max 3.83   (text only)
506 OK during storm     48% below 0.48 MB   median 0.50   none above 1.03 MB
```

Under independence ~29 of 60 failures would fall below 0.48 MB; **zero do.**
Nothing under the knee failed; above it failure is intermittent (≈264 OK vs
≈55 failed in 0.48–1.03 MB); all 5 requests above 1.03 MB failed. The floor
coincides with the ~480 KB knee measured independently by curl on 09-21.

Caveat: the absolute position of the floor depends on the tokens→bytes factor;
the existence of a clean floor does not (both groups share it).

**CLI version ruled out on this box:** every admin session 09-16 → 09-21 ran
2.1.252 (control: 2.1.282 appears after today's restart).

**Still open:** onset. The same session ran ~3.8 MB bodies clean all of 09-17.
Best current model is Sven's "two things": a path fault with onset ~09-18,
bursty thereafter, tripped by request bodies that routinely exceed the knee.

### 21. Sven's N / per-day / onset checks — and a correction to #18

Script: `/tmp/reqsize.py` (per-request size, failures = last good + appended
content; the 6 August API errors were "OAuth session expired" and are excluded).

* **Correction to #18:** "09-20 00:31 → 09-21 10:19 max ≈ 1.0 MB" was wrong.
  That query stopped at 09-21 00:00. Between 09-21 06:00 and 10:19 there were
  109 more clean requests at 1.03–1.55 MB.
* **N:** 166 band requests (0.48–1.03 MB) ran clean after the last failure
  (09-20 00:31). 0.83^166 ≈ 4e-14, so a steady fault at storm odds is ruled out.
* **Band fail rate by day:** 09-18 35% (35/99), 09-19 10% (16/168), 09-20 2%
  (3/190, all before 00:31). The rate itself fell, so the fault eased.
  Traffic mix alone doesn't explain it.
* **Tension:** the curl run at 09-21 10:19 was still 0/10 at 2 MB. What fits:
  the size threshold rose while the fault stayed live (failures from 0.48 MB on
  09-18; clean up to 1.55 MB on the morning of 09-21; 2 MB still failing). This
  is inferred, not measured: before the cap, the CLI never sent anything between
  1.55 and 2 MB. After the sndbuf cap, 94 requests at 2.0–2.4 MB ran clean.
* **"5/5 above 1.03 MB failed"** were all the 3.8 MB compaction session. They
  don't cover 1.03–2 MB.
* **Onset box:** the last clean request above 1.03 MB was 09-17 18:38:17
  (3.80 MB). The first failure was 09-18 10:19:39 (3.81 MB, same session). No
  requests in between, so the window is 15.7 h overnight. Check router log /
  ISP status for that window.

### 22. Onset-window checks (09-17 18:38 → 09-18 10:19)

* Retract the 0.83^166 ≈ 4e-14 figure from #21. It assumes the storm-wide 17%
  rate held, but the day split shows the rate wasn't constant. The evidence is
  the fall itself: 35% → 10% → 2%.
* This Mac is clean for the window. `softwareupdate --history` shows nothing
  since 04-28, and there has been no reboot since 07-30. The only package
  installed in the window was an XProtect data plist at 09-18 03:20.
  The unified log no longer covers the window.
* Router: an Xfinity gateway at 10.0.0.1, on Wi-Fi (en1). Its uptime and
  firmware date need the admin login, which we don't have. Asked Eric.
  Sven's point: a fault that fades gradually over 3 days fits a path problem
  (ISP, modem or router) better than an on/off OS change.
* The 384KB sndbuf cap is nearly free. 384KB per ~16.7 ms RTT is about 23 MB/s,
  roughly 5× the 4.7 MB/s uplink, and it only limits throughput for RTTs above
  ~85 ms. No rush to remove it.
* 13:05: Eric said he restarted the gateway "like yesterday". Its uptime now
  starts from that restart, so it can't date the onset. Still unknown: whether
  there was also a restart on the night of 09-17/18. Firmware date is
  available in the Xfinity app. Connectivity after the restart is fine
  (gateway RTT ~8 ms).

### 23. The fault survives a gateway reboot (2026-09-24 ~13:10)

`scripts/sndbuf_ab.py <SO_SNDBUF> <runs>` (env `H`/`P` choose host/path).
It uploads 2 MB with an explicit per-socket SO_SNDBUF, which gets around
`autosndbufmax` without touching the system cap.

```
speed.cloudflare.com  SO_SNDBUF 4 MB    0/10   9x ECONNRESET @15.3s, 1x TLS "bad record mac" @0.2s
                      SO_SNDBUF 384 KB 10/10   0.5s
                      default (132 KB)  5/5    0.5s
```

Run a few minutes after Eric rebooted the Xfinity gateway. A reboot doesn't
clear the fault, so it sits in the gateway firmware or at Comcast.

**New lead:** one TLS `bad record mac`. The server got a record that had been
altered in transit yet still passed the TCP checksum. That suggests something
on the path is rewriting packets, not just dropping them. It's one instance,
so this isn't confirmed.

**Like-for-like attempt failed:** the same test against api.anthropic.com
(count_tokens, no key) returns 401 in 0.4s at both buffer sizes. The server
answers on the headers, and a 4 MB buffer holds the whole 2 MB, so a 401
doesn't show the body was delivered. The Cloudflare and Anthropic hosts have
similar RTT (connect ~18 ms vs ~17 ms) but are different networks. "The knee
moved up" from #21 is still a guess.
* **IPv4 fails the same way** (`FAM=4 scripts/sndbuf_ab.py 4194304 10`): 0/10,
  ECONNRESET at 15.3s. 384 KB: 5/5. So the problem isn't specific to the IPv6
  path, and the gateway's NAT/conntrack is ruled out, since IPv6 isn't NATed.
  What's left is common to both: the Mac's Wi-Fi/NIC (checksum offload means
  bad bytes can still carry a valid checksum), the gateway's routing/modem,
  and Comcast.
* **Next steps, which need Eric:** run `sudo scripts/uplink-pcap`. It captures a
  failing run on v6 and v4, prints the RST TTL/hlim (64 or 255 = gateway;
  same as the SYN-ACK = server; anything else = in between), and shows whether
  the Mac kept retransmitting. Then rerun the test over a phone hotspot to
  separate the Mac from the path.
* **Withdrew the sudo-script ask.** Sven pointed out that asking the admin to
  `sudo` a script in `~/dispatch` runs as root whatever the file says at that
  moment, and anything that can edit that folder can change it. Deleted
  `scripts/uplink-pcap`. Instead, Eric types a one-liner himself:
  `sudo tcpdump -i en1 -s 128 -G 60 -W 1 -w /tmp/rst.pcap host speed.cloudflare.com`
  A watcher (`/tmp/pcap_watch.sh`) fires one failing upload on v6 and one on v4
  as soon as the file appears. Reading the pcap needs no root.
  **Rule: never ask the admin to sudo a file that an agent can write. Give
  them a command they type themselves.**
* **Sven's 4-way split** (Mac mini, built-in Ethernet en0 is available). Do a
  baseline Wi-Fi run first, then compare hotspot and cable:
  both fail = Mac's network stack; hotspot fails and cable passes = Mac's Wi-Fi;
  both pass = gateway's Wi-Fi; hotspot passes and cable fails = gateway
  modem/routing or Comcast. This assumes the phone's path doesn't have the
  same fault.

### 24. pcap of a failing run: the Mac never retransmits (2026-09-24 13:46)

Eric's capture: `tcpdump -i en1 -s 128 -G 60 -W 1 -w /tmp/rst.pcap host
speed.cloudflare.com`, copied to `state/rst-2026-09-24.pcap`. It has one
failing 4 MB-buffer run each on v6 and v4. (As Sven predicted, `-G` only exits
when a packet arrives. A tiny `curl -4 https://speed.cloudflare.com/` after
the 60s made it exit and flush.)

```
                     SYN-ACK hlim/ttl  RST hlim/ttl  RST after last Mac pkt
IPv6                 53                53            14.98 s
IPv4                 53                53            14.99 s
```

* **The server sent the RST.** Its hop count matches the SYN-ACK. It gave up
  after 15s of no progress, which is the 15.3s signature.
* **Sequence (v6):** the Mac bursts all 2 MB within ~340 ms of the SYN. One
  segment (seq 1310515, 1348 B) is seen leaving en1 but never reaches the
  server. The server sends **101 dup ACKs** (`ack 1310516`, SACK
  `1311864:2101733`), and all of them arrive at en1. **The Mac sends nothing
  for 15s, with zero retransmits.** IPv4 is the same: hole at 1143931, 74+
  dup ACKs, no retransmit.
* **Reading:** losing one packet in a 2 MB burst happens somewhere after en1
  (Wi-Fi, gateway or Comcast) and is ordinary. What's fatal is that the Mac
  never recovers, unless its retransmits are dropped below the BPF tap. The
  cap works by keeping bursts small. The path's loss rate varies, which fits
  the storm fading over three days. No system extensions and no VPN are
  loaded.
* **Oddity:** the Mac's TCP TSval is frozen for the whole connection: v6 has
  1565 packets over 342 ms, all TSval 3414238356; v4 has 1521 packets over
  302 ms, all 2298329629. The server's TSval changes (270 distinct values). A
  TCP clock that isn't ticking could stop retransmit timers from firing.
  Still needed as a control: the same capture of a passing 384 KB run. If
  TSval is frozen there too, it's normal and not the cause.

### 25. ROOT CAUSE: macOS 49.7-day tcp_now freeze (found by Sven)

XNU's TCP clock `tcp_now` is a 32-bit millisecond counter. After 2^32 ms of
uptime (49d 17h 02m 47.3s), a monotonicity guard stops it from wrapping, so
it freezes. Photon published this in April 2026, and there was no Apple fix as
of those reports. A reboot is the only known fix. With a frozen clock:
TSval stays constant, time-based loss recovery never fires, and TIME_WAIT
sockets never expire, so ephemeral ports eventually run out.

**Evidence on this box:**
* Booted 2026-07-30 12:11:14 PDT + 2^32 ms = **2026-09-18 05:14:01 PDT**. That
  falls inside the onset box from #21 (09-17 18:38 → 09-18 10:19).
* The pcap in #24 shows a frozen TSval on every Mac packet, and 101 dup ACKs
  with no retransmit. That's the mechanism.
* `netstat -an -p tcp` shows **19,536 TIME_WAIT** (plus 5,018 LAST_ACK and 1,522
  FIN_WAIT_1). Sven's healthy box, up 23 days, has 1.

**What we got wrong along the way:** sysctl tuning, the router, Comcast, and
"the fault eased" (the daily error rate fell as the guard kept bodies smaller
and the sessions changed; the fault itself never went away). The
`autosndbufmax` cap worked because smaller bursts lose fewer packets, not
because the network was fixed.

**Fix:** reboot, then rerun `FAM=6 scripts/sndbuf_ab.py 4194304 10` (should
pass), check the TSval is advancing, and check `netstat -an -p tcp | grep -c
TIME_WAIT` is small. After that, reboot at least every ~45 days. Worth adding
an uptime alert at 40 days. The `autosndbufmax` cap can come out after it's
verified.

Sources: https://photon.codes/blog/we-found-a-ticking-time-bomb-in-macos-tcp-networking ,
https://tidbits.com/2026/04/09/why-the-macos-49-day-networking-bug-probably-wont-affect-you/
* **Pre-reboot evidence saved** to `state/pre-reboot-tcp-2026-09-24.txt`
  (boottime + TCP state counts) and `state/rst-2026-09-24.pcap`.
* **The reboot is not safe to do unattended.** FileVault is off, but
  **auto-login is off** (no `autoLoginUser`, no `/etc/kcpassword`). After a
  reboot the Mac stops at the login window, so the user LaunchAgents (the
  daemon) don't start until someone logs in. Past record: the 06-27 reboot sat
  at the login window until 07-01 (4 days). Eric said "if you're sure you'll
  recover, go for it", so I held off and gave him two options: reboot while
  he's at the Mac, or enable auto-login so I can reboot it myself.
* **Port-exhaustion clock (13:54):** tcp6 has 14,533 of 16,384 ephemeral
  local ports in use, rising ~6/min (≈1,850 left, ~5 h). tcp4 has 11,955.
  Top v6 TIME_WAIT destinations: Google 2607:f8b0:4005:8 (1,601), Comcast
  2001:558:feed:44 (1,067), speed.cloudflare.com (947, which is the uplink
  probe plus today's tests), Anthropic 2607:6bc0::10 (662). Told Eric it has
  to be done today.
* **Fallback if the reboot can't happen today (Sven):** `sudo sysctl -w
  net.inet.ip.portrange.first=32768` roughly doubles the ephemeral range and
  buys about 2 days. It lasts until the next reboot and doesn't fix anything.
  Checked: nothing listens on TCP or UDP in 32768–49151, so it won't collide
  with local services.

### 26. Verified fixed by reboot (2026-09-24 14:07:44)

Eric rebooted at 14:07:44, and the daemon and sessions came back.
* `FAM=6 scripts/sndbuf_ab.py 4194304 10`: **10/10**. `FAM=4 ... 5`: **5/5**.
  Both were 0/10 before the reboot.
* TIME_WAIT: 19,549 before, 29 after. Of 11 TIME_WAIT sockets seen, 0 were
  still there 45s later, so they expire again.
* TSval ticking was not checked directly (a pcap needs sudo). The upload and
  TIME_WAIT results are enough.
* **Next freeze:** 2026-11-13 ~07:10 PDT (boot + 2^32 ms). Reboot by ~11-08.
  **TODO:** add an uptime alert at 40 days. Consider removing the
  `autosndbufmax=393216` cap from `/etc/sysctl.conf` (needs sudo). It costs
  little, but it was covering for this bug.
* **Guard added (Sven's idea):** a `tcp_clock` dependency check in
  `assistant/dependency_checks.py`, running every 30 min.
  - TIME_WAIT > 1,000 → DOWN. Healthy is under 30; a frozen clock crosses
    1,000 in ~3h.
  - Uptime ≥ 45d → DEGRADED with the days left before the freeze.
  - Both escalate by SMS (no local recovery; phone-sized text) and repeat
    every 12h.
  Tests: `tests/test_tcp_clock_probe.py` (8 pass). The suite's failures are
  the same unrelated ones as before. The daemon started before this edit
  (14:07 reboot), so it goes live on the next daemon restart. Not committed.
* **Escalation path verified without paging Eric:** ran the real
  `build_default_registry` runner and `make_sms_escalator` with
  `TCP_CLOCK_TIME_WAIT_DOWN=-1`, stubbing only `Manager._send_sms`. The first
  run went HEALTHY→DOWN→ESCALATED and produced 1 SMS to the owner phone.
  Runs 2–3 sent nothing more (the 12h re-escalation window holds). The real
  send is the same `Manager._send_sms` path that api_uplink used to page Eric
  16 times on 09-21/22. The SMS is 233 chars including the runner's
  "[SVEN] 🚨 dependency…" prefix and "(no automatic recovery available)"
  suffix.

## 2026-09-25 — package-watch "arriving today" digest landed after delivery
Symptom: Eric asked "Eta on today's packages" at 10:40 PT; both TR boxes had been delivered
at 10:05 and he'd had no morning text. Log: 08:15 run -> "before 9:00 — holding digest".
Root cause: DIGEST_HOUR=9 but launchd's first slot is 08:15 and the next is 12:15, so the
morning digest could never fire before noon. Fix: DIGEST_HOUR=8 (bin/package-watch), plus a
regression test asserting DIGEST_HOUR <= first StartCalendarInterval hour. Also fixed an XML
comment in the plist containing "--" (invalid XML; plistlib refused it, launchd tolerated it).
Separately: bin/track returned "unknown" for both FedEx numbers today while fedex.com in
Chrome showed "Delivered 9/25 10:05 AM". Not yet investigated.

## 2026-09-25 — "Still unresponsive": lock screen was real but not the cause; APNs stall re-confirmed with today's numbers
Eric reported two things: the Mac keeps locking (which blinds every GUI-driven skill
with no alert) and the agent "still seems quite unresponsive".

**Lock screen (fixed, self-healing).** `pmset displaysleep 10` + `sysadminctl
-screenLock` delay 300s = locked ~15 min after the last input; Chrome Remote Desktop
keeps it awake only while connected. Added `bin/screen-lock` (status / unlock /
harden / lock) and a `screen_lock` dependency check (60s). Locked → DOWN → type the
Keychain login password (`mac-login-password`) into loginwindow via a CGEvent HID tap →
SMS with the exact ask if that fails. `screen_lock.harden: true` (opt-in) also turns off
the re-arming settings. Unit tests in `tests/unit/test_screen_lock_probe.py`. The unlock
typing path is NOT yet exercised live (Eric was on CRD; locking his session to test it
would have been rude) — first real lock will tell. Note the auto-mode classifier blocks
`screen-lock harden` and any `sudo` with the password; those stay owner-run.

**Unresponsiveness (diagnosed, needs sudo to fix).** Inbound lag = bus
`message.received` time minus the message's own `timestamp_ms`:
    09-25 10:24 "Eta on today's packages"        997s
    09-25 11:44 "Am I possibly missing the plate" 786s   (+ photo: 582s)
    09-24: 941 / 1294 / 228 / 698 / 670 / 737 / 499 / 445 / 688 / 815 / 578s
Every one of the 9 STALENESS spikes in the last 24h lands within 1s of imagent
`APS Connection status did change to connected: NO -> YES`. This is the 09-21
CORRECTION's finding again, now with live logs: **apsd and imagent DO emit unified-log
lines on this box** (the 09-21 note saying they don't is stale — use
`/usr/bin/log show --predicate 'process == "imagent" AND eventMessage CONTAINS "APS"'`;
note `log` is a zsh builtin, call `/usr/bin/log`). apsd's own keepalive is in
`PCMultiStageGrowthAlgorithm` state Backoff at 739.9s (grew from 720 toward 1480s), so
a mapping that dies right after a keepalive is only noticed ~12–13 min later — the
786s stall is exactly that.

**The 09-21 kernel-keepalive mitigation is unverified and probably inert.**
`net.inet.tcp.always_keepalive=1` / `keepidle=180000` are set (and survived the 09-24
reboot via /etc/sysctl.conf), yet `netstat -s -p tcp` shows `0 keepalive probe sent`,
`0 keepalive timeout` after 22h — and stays 0 during a controlled 20s test with an
explicit per-socket 3s keepalive. Either the counters or the probes are missing; only
tcpdump can tell and that needs root. `scripts/tcp-keepalive-check` (run with sudo)
gives the verdict in 25s.

**The wifi-bounce stopgap never worked.** `bin/aps-watchdog` (LaunchAgent
`com.dispatch.aps-watchdog` — which also violates the two-LaunchAgents rule) logged 0
BOUNCE lines in 17 days across dozens of qualifying stalls: bash spawned by launchd has
no Full Disk Access, `sqlite3` on chat.db fails with "authorization denied", and
`2>/dev/null` hid it. It now logs that error hourly. Whether to run it at all is Eric's
call — cycling Wi-Fi also drops his CRD session and any in-flight API turn.

**Next (owner):** `sudo scripts/tcp-keepalive-check`. If probes flow, tighten
`net.inet.tcp.keepidle=60000 keepintvl=10000 keepcnt=3` (and persist in
/etc/sysctl.conf) so the NAT mapping is refreshed every minute and a dead one is
detected in ~90s. If no probes, the kernel path is dead and the options are the
wifi bounce (run from a Full-Disk-Access context) or a router/NAT idle-timeout fix.

**Lesson:** the sensible-sounding mitigation from 09-21 was applied and declared
"UNVERIFIED" — and then nobody verified it, so four more days of 13-minute stalls
looked like "the agent being flaky". A mitigation without a measurement is a hope.

## 2026-09-26 — digest kept listing delivered parcels
Symptom: 08:15 digest listed 3 parcels delivered on 9/24-9/25. Eric: "Why are you pinging me
about shit delivered already". Root cause: delivered entries were never retired; every daily
digest re-listed them (hidden until yesterday's DIGEST_HOUR fix made the morning digest fire).
Fix: a delivery is reported once (day-of, state key `<num>:delivered_reported`), and delivered
entries from earlier days are silently retired from package-watch.json. 3 tests added.
Also found: TestItActuallyRuns ran the real script against the LIVE config (could text Eric a
real digest from pytest) and notify tests wrote fake SEND FAILED lines to the live log.
Fixed with PACKAGE_WATCH_STATE_DIR / PACKAGE_WATCH_LOG env overrides + autouse log fixture.

## 2026-09-27 — Mac sat locked 14h; alerts every 2h said "typing the password"
08:29 screen_lock auto-unlock failed 3x (cgevent, fell back to system-events) on what looks
transient; runner went ESCALATED and, by design, never called recover() again — it only
re-paged "STILL DOWN … Typing the Keychain login password" every 2h, which was false. Unlocked
by hand ~22:40. Since then the lock re-arms every ~11 min (display sleep 10 min + require
password 300s) and auto-unlocks in ~5s, which trips the "self-recovered 3x/hour" alarm.
Fix: DependencyCheck.retry_while_escalated_s (screen_lock=600): one recover() every 10 min while
ESCALATED; the clock starts at escalation. 2 tests. Real fix pending Eric's OK: screen_lock.harden
(turn off require-password-after-sleep; same trade-off as the auto-login he approved).
Also: 00:00:45 "Sev0 chrome-housekeep replayed" = bus.db "database is locked" delayed the offset
commit, redelivery was blocked by dedupe. Harmless; ~1/day. Alert severity is overstated.
2026-09-28 00:40: Eric said "Yeah" → screen_lock.harden: true in config.local.yaml; `screen-lock harden`
applied (require-password = off). Also fixed bin/screen-lock harden: it passed the login password
in argv (`sysadminctl ... -password <pw>`, visible in `ps`); now `-password -` answered over a pty.
2026-09-28 01:17 — harden alone did NOT stop it: locks continued at 00:40/00:51/01:02 with
`sysadminctl -screenLock` = off (loginwindow window verified on screen at 01:02:37). Each lock
came ~10 min after the last input = display sleep. Test: `caffeinate -d` for 14 min → zero locks.
Fix: LaunchAgent com.dispatch.keep-display-awake (caffeinate -d, KeepAlive). Also: unlock now
re-checks the lock right before typing, so the password can never be typed into a frontmost app
(checked chat.db: no outgoing message ever contained it).

## 2026-10-01 — false "you have not sent ANY reply" nudges (x2 in 30 min)
Sessions alias the reply CLI (`R=~/.../reply; $R "msg"`); `_is_send_command` only looked at literal
first tokens, so `$R` sends never updated last_send_at and the unanswered-inbound watchdog fired at a
session that had replied within 20s. Fix: track VAR=value in the command and expand `$VAR`/`${VAR}`
before matching. Tests in test_write_ahead_queue.py::TestSendCommandAliased.

2026-10-02: plate re-announced a day late; retire logic read eta, single-package FedEx parser sets delivered_at. Fixed + test.

## 2026-10-04 — Stale sven-pages guide on Eric's phone
Republished audio-wiring with inline images, but Eric still saw the old page: the worker served all files with `max-age=3600`. Fix: HTML is now `no-cache` (svenka-pages-worker `cacheControlFor`). Also, the guide named cables by brand ("UGREEN") without saying what they are. Lesson: in how-to guides, describe each part by what it looks like, not just its brand.

## 2026-10-12 — cherry-eric group silent for ~40 min (stale resume id)
The group's registry `session_id` (aaf9b52d…, from May) pointed to a deleted Claude transcript. Each inbound
message tried RESUME_FROM_REGISTRY, the CLI exited with "No conversation found with session ID", and the message
went to the DLQ after 2 retries. Health checks reported 5/5 healthy because the session never got registered as
alive. Eric: "Why the heck aren't you replying in the group chat."
Fix: `claude-assistant restart-session <group> --clean`, then injected a catch-up prompt. Code:
`sdk_backend.resume_conversation_exists()` + `SDKBackend._validated_resume_id()`. Both create paths
(individual + group) now drop a resume id whose `~/.claude/projects/*/<id>.jsonl` is gone, log RESUME_MISSING,
clear the registry id and start fresh. Tests: tests/unit/test_resume_missing.py.
