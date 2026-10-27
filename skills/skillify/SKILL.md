---
name: skillify
description: Analyze conversations to propose new skills, improvements to existing skills, and skill merges. Mines transcripts for automation opportunities, recurring workflows (cross-session AND within-session), repeated primitives over multiple days, tool gaps, and skill quality issues. Re-evaluates previously refuted proposals when new evidence appears. Discovery subagent reads all chats holistically, then parallel refinement subagents evaluate each idea. Trigger words - skillify, propose skills, what should we automate, skill opportunities, improve skills, merge skills.
---

# Skillify

Analyze recent conversations to:
1. **Propose new skills** to automate recurring workflows, fill tool gaps, or build discussed ideas
2. **Suggest improvements to existing skills** based on user corrections, workarounds, and back-and-forth that indicates a skill's instructions are incomplete
3. **Suggest skill merges** when two or more existing skills overlap significantly or would be better as a single unified skill

## PII Policy

**NEVER suggest putting PII (names, phone numbers, emails, IPs, hostnames, URLs with hostnames, account usernames) directly into SKILL.md files, CLAUDE.md, or any checked-in code.** All identity and infrastructure values MUST be stored in `~/dispatch/config.local.yaml` (which is gitignored) and accessed via:

- In markdown/skill files: `!``identity dotpath`` ` dynamic prompts (resolved at load time by Claude Code)
- In Python: `from assistant.config import get; get("dotpath")`
- In shell: `~/dispatch/bin/identity dotpath`

When proposing fixes or new skills, if the proposal would include any PII or infrastructure-specific values (IPs, hostnames, URLs, usernames, real names), instead:
1. Add the value to `config.local.yaml` under an appropriate key
2. Reference it via `identity` in the skill/doc
3. Update `config.example.yaml` with a placeholder template

Examples of values that MUST go through identity:
- IP addresses, Tailscale hostnames, local network IPs
- Account emails, usernames, phone numbers
- Service URLs with hostnames (Plex, Caddy proxy, etc.)
- Real names of contacts or the system owner
- API keys, tokens (use keychain or secrets.env instead)

## When to Use

- User says "skillify", "propose skills", "what should we automate"
- Nightly consolidation passes "--nightly" flag

## How It Works

### Step 1: Identify Active Sessions

```bash
cat ~/dispatch/state/sessions.json
```

- Filter to sessions active in the last 24 hours
- Include both 1:1 sessions and group chats, but for group chats only include those where the admin user is a participant (check the `participants` array)
- **IMPORTANT**: Some sessions have stale/missing `last_message_time`. Cross-check by reading recent messages for sessions with empty timestamps but known activity (e.g., sessions with "sven" in the name)
- Sort by most recent activity
- Cap at **10 sessions**

If no active sessions, report "nothing to analyze" and stop.

### Step 2: Discovery (Single Subagent — Holistic Cross-Chat Analysis)

Spawn **one discovery subagent** (use `model="sonnet"`) that reads ALL active session transcripts and identifies raw skill ideas. This must be a single subagent because skill opportunities often span multiple conversations.

The discovery subagent:

1. **Reads each transcript** sequentially:
   ```
   uv run ~/.claude/skills/sms-assistant/scripts/read_transcript.py --session {SESSION_NAME} --limit 50
   ```
   If a transcript read fails, skip it and note the skip.

2. **After reading ALL transcripts**, identifies skill opportunities using these signals:

   **HIGH SIGNAL — explicit automation intent:**
   - User explicitly asked for a tool/CLI/skill that doesn't exist
   - User brainstormed a tool in detail and deferred implementation
   - Examples: "I want a CLI that does X", "we should build a skill for this", "don't implement yet"

   **MEDIUM SIGNAL — implicit recurring need:**
   - User expressed frustration with manual work
   - Same category of work appearing across multiple chats (e.g., WebGPU benchmarking in one chat + WebGPU debugging in another = unified tooling opportunity)
   - Agent did lengthy manual work that a reusable script could handle
   - Existing skill was used but failed or needed workarounds
   - **A repeatable multi-step process was followed across chats** — even if it looks like "normal coding", ask: "Is this a process that could be applied to OTHER inputs?" If the same workflow (e.g., convert model → build shaders → benchmark → verify correctness) was done for Model A in one chat and Model B in another, that's a SKILL, not just coding.
   - **Within-session repeated primitives** — the pattern does NOT have to span multiple sessions. If the same multi-step primitive (e.g., "fetch URL → extract data → format → save") was done 3+ times within a SINGLE session, that's a strong skill signal. The repetition makes it automatable regardless of whether other sessions do it too.
   - **Repeated primitives over multiple days** — check previous days' transcripts (via old skillify reports and bus history). If a user does the same manual steps on Monday, Wednesday, and Friday across different sessions, that's a skill candidate even if each individual session only did it once. Cross-reference with historical skillify reports to detect multi-day patterns.

   **SKIP:**
   - Truly one-off coding tasks (fixing a specific bug, adding a feature to a specific app)
   - One-off questions or tasks
   - Idle chat, greetings, jokes
   - Tasks already handled by existing skills

   **IMPORTANT — "normal coding" is NOT automatically a skip.** Coding that follows a repeatable process applicable to new inputs IS a skill opportunity. The question is: "Could someone say 'do this again but for X' and the same steps would apply?" If yes, it's a skill candidate.

   **SKILL IMPROVEMENT signals — look for these patterns that indicate an existing skill needs updating:**
   - User corrected the agent's behavior after a skill fired ("no, that's wrong, do X instead")
   - Lots of back-and-forth to get a skill-triggered action right (>2 corrections for one task)
   - User manually fixed or adjusted output from a skill-driven action
   - User provided extra instructions that should already be in the SKILL.md ("always do X when Y")
   - Workarounds that suggest missing edge cases ("it doesn't handle Z, so do this instead")
   - Agent apologized for doing something wrong that a skill should have prevented

   **SKILL MERGE signals — look for skills that should be combined:**
   - Two skills that cover overlapping domains (e.g., separate skills for "image-to-video" and "video-generator" that both call the same APIs)
   - Skills that are always used together and would be simpler as one (e.g., a "fetch" skill and a "parse" skill that are never used independently)
   - Skills with confusing boundaries where users/agents pick the wrong one (e.g., "hue" vs "lutron" might benefit from a unified "lights" skill)
   - Small skills that are too narrow to justify their own SKILL.md and would fit naturally inside another skill
   - Multiple skills wrapping the same underlying tool/API with slight variations

3. **Returns THREE lists:**

   **a) New skill ideas** — each with:
   - `name`: kebab-case
   - `description`: 1-2 sentences
   - `signal`: high or medium
   - `evidence`: direct quote(s) from transcript(s)
   - `context`: 2-3 sentences on what user was trying to accomplish
   - `source_sessions`: list of session names this idea came from
   - `type`: "new_skill"

   **b) Skill improvement ideas** — each with:
   - `skill_name`: name of existing skill that needs improvement
   - `issue`: what went wrong or what's missing
   - `evidence`: direct quote(s) showing the correction/workaround/frustration
   - `suggested_fix`: what should change in the SKILL.md
   - `source_sessions`: list of session names
   - `type`: "skill_improvement"

   **c) Skill merge ideas** — each with:
   - `skills_to_merge`: list of 2+ skill names that should be combined
   - `proposed_name`: kebab-case name for the merged skill
   - `rationale`: why these skills belong together (overlapping functionality, always used together, confusing boundaries, etc.)
   - `evidence`: examples from transcripts where the overlap caused confusion, or structural analysis showing redundancy
   - `type`: "skill_merge"

### Step 3: Refinement (Parallel Subagents — One Per Idea)

For each raw idea from discovery (new skills, skill improvements, AND skill merges), spawn a **refinement subagent** (use `model="sonnet"`) in parallel.

**Run them in the FOREGROUND, all in ONE message** (one assistant turn containing every Task call, so they run concurrently and you get all results back in that turn). Never set `run_in_background`, and never use `ScheduleWakeup` or `/loop` to wait for them, ESPECIALLY in nightly mode. The nightly run is an ephemeral task session. On 2026-10-18 the run used background subagents plus `ScheduleWakeup`, and the task runner idle-killed it mid-pipeline. The admin SMS was never sent, but the run still reported success.

#### For new skill ideas, each refinement subagent:

1. **Checks existing skills** — reads `sed -n '/^---$/,/^---$/p' ~/.claude/skills/*/SKILL.md` to get frontmatter for all existing skills. Decides: is this idea already covered?

2. **Checks previous proposals** — reads `cat ~/dispatch/state/skillify-proposals.json 2>/dev/null || echo '{"runs":[]}'`. Has this been proposed before?

3. **Makes a verdict**: **ACCEPT**, **REFINE**, or **REFUTE**
   - ACCEPT: idea is novel, reusable, and worth building as-is
   - REFINE: idea has potential but needs to be rescoped, narrowed, or reframed to be viable. The subagent rewrites the idea with a better framing and re-evaluates. For example: "too broad as stated, but if scoped to just X it becomes viable" → rewrite and accept
   - REFUTE: idea is a clear duplicate of an existing skill, or fundamentally not automatable. Must cite specific reason. **Be conservative with refute** — if there's any way to rescope the idea into something useful, use REFINE instead. Only refute for true duplicates or ideas that genuinely cannot be a skill.

4. **If accepted or refined**, fleshes out into a full proposal:
   - **name**: kebab-case
   - **description**: 1-2 sentences with trigger words (ready for SKILL.md frontmatter)
   - **seed_spec**: one sentence fully describing what to build — self-contained enough to hand off cold
   - **key_instructions**: 2-3 bullet points on core functionality
   - **complexity**: small (SKILL.md only) / medium (+ scripts) / large (+ external deps)
   - **source_sessions**: which session(s) this came from
   - **evidence**: direct quote(s)
   - **context**: 2-3 sentences
   - **overlap_check**: "Checked [skill-name]: not covered because [reason]" or "No similar existing skill"

#### For skill improvement ideas, each refinement subagent:

1. **Reads the existing skill's SKILL.md** — `cat ~/.claude/skills/{skill_name}/SKILL.md` to understand current instructions

2. **Evaluates the issue** — is this a real gap in the skill's instructions, or was it a one-off misunderstanding?

3. **Makes a verdict**: **ACCEPT**, **REFINE**, or **REFUTE**
   - ACCEPT: the skill's SKILL.md is genuinely missing guidance that caused the issue. Propose specific language to add.
   - REFINE: the issue is real but the fix needs to be reframed (e.g., the problem isn't the skill itself but how it interacts with another skill)
   - REFUTE: the issue was a one-off, the skill already covers this case, or the fix doesn't belong in the skill

4. **If accepted or refined**, produces an improvement proposal:
   - **skill_name**: which skill to update
   - **issue**: what went wrong
   - **proposed_change**: specific text to add/modify in the SKILL.md (show the actual language, not just "add error handling")
   - **evidence**: direct quote(s) showing the problem
   - **source_sessions**: which session(s)

5. **Returns** the verdict and proposal.

#### For skill merge ideas, each refinement subagent:

1. **Reads both/all skills' SKILL.md files** — `cat ~/.claude/skills/{skill_name}/SKILL.md` for each skill in the merge proposal

2. **Analyzes overlap** — how much functionality is shared? Are the trigger words confusing? Do sessions ever use the wrong one? Would merging reduce confusion or create a bloated mega-skill?

3. **Makes a verdict**: **ACCEPT**, **REFINE**, or **REFUTE**
   - ACCEPT: the skills clearly overlap, merging would reduce confusion and improve discoverability
   - REFINE: partial merge makes sense (e.g., merge 2 of 3, or extract shared logic into a base skill)
   - REFUTE: the skills are distinct enough that merging would make the combined skill too broad or confusing

4. **If accepted or refined**, produces a merge proposal:
   - **skills_to_merge**: list of skill names
   - **proposed_name**: name for the merged skill
   - **rationale**: why merging improves the system
   - **migration_plan**: what happens to the old skills (delete, redirect, alias)
   - **proposed_structure**: outline of the merged SKILL.md (sections, trigger words)

5. **Returns** the verdict and proposal.

### Step 4: Re-evaluate Previously Refuted Proposals

**CRITICAL: Before finalizing refinement results, check if any previously refuted proposals should be reconsidered.**

1. Load historical proposals: `cat ~/dispatch/state/skillify-proposals.json 2>/dev/null`
2. For each refuted proposal from the past 30 days:
   - Check if today's transcripts contain NEW evidence supporting the refuted idea
   - Check if the refutation reason is still valid (e.g., "already covered by skill X" — but skill X was since deleted or changed)
   - Check if the idea appeared again independently (same concept, different phrasing)
3. If new evidence exists, create a refinement subagent to re-evaluate with the combined old + new evidence
4. If re-accepted, mark it with `"previously_refuted": true`, `"original_refute_date": "..."`, `"original_refute_reason": "..."`, and `"new_evidence": "..."` so the admin sees the full history
5. If still refuted after re-evaluation, drop it silently (don't re-report in Refuted section — it was already reported)

This ensures good ideas that were initially refuted due to insufficient evidence get a fair second look as more data accumulates.

### Step 5: Rank, Persist & Present

Collect results from all refinement subagents (Steps 3 and 4):
- Drop refuted ideas (except re-evaluated ones that were re-accepted)
- Rank accepted proposals by type, then within each type:
  - **New skills**: high signal first, cross-chat/multi-day patterns ranked higher, smaller complexity first
  - **Improvements**: by severity of the issue (user frustration level, frequency of occurrence)
  - **Merges**: by degree of overlap and confusion caused
  - **Re-evaluated**: always shown separately regardless of ranking
- Cap at **5 proposals per type** (new skills, improvements, merges counted separately)

**Persist** to `~/dispatch/state/skillify-proposals.json`:

```json
{
  "runs": [
    {
      "date": "2026-03-13",
      "proposals": [
        {"type": "new_skill", "name": "...", "signal": "high", ...},
        {"type": "skill_improvement", "skill_name": "...", ...},
        {"type": "skill_merge", "skills_to_merge": [...], "proposed_name": "...", ...},
        {"type": "new_skill", "name": "...", "previously_refuted": true, "original_refute_date": "2026-03-08", "new_evidence": "...", ...}
      ],
      "sessions_analyzed": 5,
      "skipped_sessions": [],
      "refuted": [{"name": "...", "reason": "...", "type": "new_skill|skill_improvement|skill_merge"}]
    }
  ]
}
```

All proposal types go in the same `proposals` array, distinguished by their `type` field. Append new run to the `runs` array. If more than 30 runs, drop the oldest.

**Present** results in three sections:

**## New Skills**
- Show new skill proposals with full details
- For each, end with: `To build: "build the [name] skill — [seed_spec]"`

**## Suggested Skill Improvements**
- Show improvement proposals with: skill name, issue, proposed SKILL.md change, evidence
- For each, end with: `To apply: "update [skill_name] — [proposed_change summary]"`

**## Suggested Skill Merges**
- Show merge proposals with: skills to merge, proposed name, rationale, migration plan
- For each, end with: `To merge: "merge [skill-a] and [skill-b] into [proposed-name]"`

**Nightly mode** (prompt contains "--nightly"): The admin must be able to approve/deny from their phone, with the FULL report available, but **never as one giant SMS**. Eric has complained about text walls repeatedly, and `reply` blocks long messages for this reason. Do NOT use `--long` for the report. On 2026-10-14 a run did, and sent a ~4,000-char wall. The 2026-10-13 run did it right: one line plus images.

Delivery. Follow this order exactly, and do the persistence steps before any rendering or sending:
1. **Persist first:** save the run to `~/dispatch/state/skillify-proposals.json` (above), then publish the bus `scan.completed` event (see "Persist Results to Bus" below). That way the results survive even if the session dies during rendering or sending.
2. Write the full report (format below) to `/tmp/skillify-$RUN_ID.md`.
3. Render it: `~/.claude/skills/md2img/scripts/md2img /tmp/skillify-$RUN_ID.md /tmp/skillify-$RUN_ID.png --width 800`. If the report is long, split it into one file per section and render each, so the images stay readable on a phone.
4. **Send LAST:** send ONE short text (≤220 chars) with the counts and the single most important item, and attach the image(s). Run it from the admin's transcript dir:
   `cd ~/transcripts/imessage/<admin_sanitized_id> && ~/.claude/skills/sms-assistant/scripts/reply "🔧 skillify: N new, M fixes. top: <one line>. reply 'apply X fix' / 'build X'" --image /tmp/skillify-$RUN_ID.png` `reply` takes only ONE `--image`, so send each extra page as its own `reply --image /tmp/skillify-$RUN_ID-2.png` (image-only sends are exempt from the burst/length guards).
5. **Verify the send:** if the `reply` output does not contain `SENT`, retry that send once. If the retry also fails, say so plainly in your final message.
6. Never end a turn with "Next I'll…" or "Now I'll…" before the send has happened. In nightly mode, nobody will prompt you again. The task runner treats a quiet session as finished and reports a notify task that never sent as `task.failed`. Do all the steps in one continuous run, and finish only after the send succeeds.

Full report format (goes in the image, not the text):

```
🔧 skillify nightly — N new skills, M improvements, K merges

## New Skills

1. **skill-name** (signal, complexity)
Seed spec: [full seed_spec]
Key instructions:
- [bullet 1]
- [bullet 2]
Evidence: "[direct quote from transcript]"
Source: [session names]
Reply "build skill-name" to create it.

2. ...

## Skill Improvements

1. **skill-name** — [issue]
Proposed change: [specific SKILL.md language to add/modify — show the actual text]
Evidence: "[direct quote showing the problem]"
Source: [session names]
Reply "apply skill-name fix" to update it.

2. ...

## Skill Merges

1. **merged-name** — merge [skill-a] + [skill-b]
Rationale: [why these belong together]
Migration: [what happens to old skills]
Reply "merge skill-a skill-b" to combine them.

2. ...

## Re-evaluated (previously refuted, now accepted with new evidence)

1. **idea-name** — previously refuted [date]: "[old reason]"
New evidence: "[new quote/pattern from today's transcripts]"
Reply "build idea-name" to create it.

2. ...

## Refuted
- idea-name — [reason]
```

Show the FULL details for each proposal in the rendered report. The admin needs enough context to make approve/deny decisions without opening a laptop. The SMS text itself stays one line.

**IMPORTANT: Do NOT create or modify any skill files. Propose only.**

#### Persist Results to Bus

**In ALL modes**, publish a `scan.completed` event to the bus for historical tracking. In nightly mode, publish it right after saving `skillify-proposals.json` and **before** rendering or sending (see the Delivery order above):

```bash
RUN_ID=$(date +%Y%m%d-%H%M)
~/dispatch/bus/cli.py produce system \
  '{"scanner":"skillify","run_id":"'"$RUN_ID"'","mode":"nightly","duration_seconds":DURATION,"summary":{"sessions_analyzed":N,"new_skills_proposed":N,"improvements_proposed":N,"merges_proposed":N,"re_evaluated":N,"refuted":N},"findings":[ACCEPTED_PROPOSALS_AS_JSON]}' \
  --type scan.completed --source skillify --key "scan-skillify-$RUN_ID"
```

The `findings` array should include ALL accepted proposals: new skills, skill improvements, skill merges, and re-evaluated proposals — each with their full details and `type` field. Refuted items go in `summary.refuted` count only, not in findings.

This enables `bus reports --scanner skillify` to query historical scan results (stored in archive indefinitely).

### Step 6: Build/Merge on Request

**For new skills:** When the user says "build [name]" or "build the [name] skill", look up the proposal in `~/dispatch/state/skillify-proposals.json`, use the seed_spec and key_instructions to build it via the skill-builder skill, and update the proposal's status to "built".

**For merges:** When the user says "merge [skill-a] [skill-b]" or "merge [skill-a] and [skill-b] into [name]", look up the merge proposal, read both skills' SKILL.md files, create the merged skill via skill-builder, delete the old skill directories, and update the proposal's status to "merged".

**For improvements:** When the user says "apply [skill-name] fix", look up the improvement proposal and apply the proposed_change directly to the skill's SKILL.md. Update the proposal's status to "applied".

## Calibration Examples

### GOOD candidates (flag these):

1. **"I want a CLI that takes a model, converts to WebGPU, benchmarks, and compares outputs"**
   → name: model-to-webgpu, signal: high, complexity: large
   Why: explicit tool request, clearly reusable, specific enough to build

2. **Agent manually scraped 3 listing sites, parsed prices, compared — user said "do this daily"**
   → name: listing-scraper, signal: medium, complexity: medium
   Why: recurring multi-step workflow, user confirmed recurrence

3. **"we should have a thing that checks ski conditions across 4 mountains"**
   → name: ski-conditions, signal: high, complexity: medium
   Why: explicit request for a tool, clearly reusable during ski season

4. **Chat A: converted data from format X, validated output. Chat B: converted different data from format X, validated output the same way.**
   → name: data-format-converter, signal: medium, complexity: medium
   Why: BOTH chats follow the same process applied to different inputs. Even though it looks like "coding", it's a repeatable workflow. Ask: "could someone say 'do this again but for Z' and the same steps apply?" If yes, it's a skill.

5. **Agent set up CI/CD pipeline in 3 different repos the same way this week**
   → name: repo-ci-setup, signal: medium, complexity: medium
   Why: same process repeated across projects = skill opportunity

6. **Within one session, user asked agent to fetch a URL, extract specific data, format it, save it — 4 separate times for different URLs**
   → name: url-data-extractor, signal: medium, complexity: small
   Why: same multi-step primitive repeated 4x in a SINGLE session. Doesn't need cross-session evidence.

7. **User does "check flight status → format update → send to partner" every few days across different sessions**
   → name: flight-status-updater, signal: medium, complexity: medium
   Why: repeated primitive over multiple days. Each session only did it once, but the pattern spans a week.

8. **Skills `image-to-video` and `shader-video` both generate video content, have overlapping trigger words, and agents sometimes pick the wrong one**
   → type: skill_merge, proposed_name: video-generator
   Why: overlapping domains causing routing confusion. Would be better as one skill with sub-modes.

### BAD candidates (SKIP these):

1. **Agent fixed a specific bug in one repo** → truly one-off, no repeatable process
2. **"What's the weather?"** → one-off question
3. **"Turn on the lights"** → already covered by hue/lutron skills
4. **Agent read a file, edited it, ran tests for a feature** → one-off feature work, not a repeatable process

## Graceful Degradation

- **Transcript read fails** → skip session, note in skipped_sessions
- **Refinement subagent fails** → note failure, continue with other results
- **Zero candidates** → "no skill opportunities found today" (normal)
- **sessions.json missing** → report error, stop
- **proposals.json doesn't exist** → create with `{"runs": []}`
- **Context too large** → reduce --limit to 30 and continue

## Nightly Integration

Called by manager.py's nightly consolidation after person-facts and chat-context. Manager.py passes "--nightly" for SMS output mode. The manager.py hook is out of scope for this skill file.

## Historical Context (Required)

Before generating findings, you MUST gather historical context to avoid re-reporting known issues or missing important changes:

### Past Reports

Query previous scan reports from the bus for what was already found, fixed, or refuted:

```bash
cd ~/dispatch && uv run python -m bus.cli reports --scanner skillify --since 7
```

This shows past findings with their verdicts (accepted/refuted). Use this to:
- Skip ideas that were already proposed and are being tracked
- Understand patterns in what gets refuted (calibrate your signal)
- Build on previous proposals rather than starting from scratch
- **Re-evaluate previously refuted proposals** — if today's transcripts show new evidence for a refuted idea, feed it into Step 4 for reconsideration. Ideas get refuted for lack of evidence, but evidence accumulates over days/weeks. A pattern that was "one-off" 5 days ago might now have 3 occurrences.

### Recent Code Changes

Check git history for recent changes that provide context on what was built, fixed, or refactored:

```bash
cd ~/dispatch && git log --oneline --since="7 days ago" -- assistant/ bus/
cd ~/.claude && git log --oneline --since="7 days ago" -- skills/
```

This helps you:
- Understand what skills were recently created or modified (don't re-propose existing work)
- Identify automation patterns that were already landed as skills
- See what the admin was working on (provides intent behind changes)
