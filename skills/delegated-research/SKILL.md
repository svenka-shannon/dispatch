---
name: delegated-research
description: Rules for fanning research out to Task subagents so findings survive session death and cannot be fabricated. Use whenever spawning subagents to research products, prices, technical claims, or anything the user will spend money or make decisions on. Trigger words - research, compare, find the best, price check, fan out, subagent research, deep research.
---

# Delegated Research

Hard-won rules from two real failures in Sept 2026. Both cost a real user real trust.
Put **both** blocks below into every research subagent prompt, verbatim.

## Failure 1 — research evaporates when the session dies (2026-09-18)

A session ran ~3 hours of speaker research entirely in subagent context, told the user
"2 min," then the daemon restarted and everything was lost. The user had asked twice.
Recovery was only possible because subagent JSONL transcripts happened to still be on disk.

**Rule: subagents must write findings to disk INCREMENTALLY, not at the end.**

Paste into every research subagent prompt:

> Write findings INCREMENTALLY to `<abs path>.md` as you go — append each section the
> moment you finish it. Do not hold everything in memory and write at the end. A previous
> session was killed mid-research and lost hours of work; incremental writes are mandatory.

Verified in practice: mid-run checks showed 547 and 147 lines already safe on disk.
Cheap to check progress without touching the agent transcript:

```bash
wc -l ~/path/research/*.md && date -r ~/path/research/foo.md +%H:%M:%S
```

**Never** `Read`/`cat` a subagent's `.output` JSONL to check progress — it is the full
transcript and will overflow context. Check the incremental .md file instead.

### Recovering research from a dead session
Subagent transcripts persist under:
`~/.claude/projects/<escaped-cwd>/<session-uuid>/subagents/agent-*.jsonl`
with an `agent-*.meta.json` alongside giving each agent's `description`. Extract final
reports without blowing up context:

```bash
jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' agent-X.jsonl
```

Then hand the dumps to a fresh consolidation subagent — don't read 270KB yourself.

## Failure 2 — a subagent fabricated citations (2026-09-19)

A nested research subagent invented Reddit thread IDs, forum usernames, post IDs, dates,
and specific numeric results for **real papers whose abstracts it had read but whose full
texts it never fetched**. It presented them as fetched evidence. It self-corrected only
after the fact — after the findings had already been relayed to the user.

This is the dangerous failure mode: the fabrications were *plausible and well-formed*, and
sat next to genuinely verified material. Specific numbers and IDs are exactly what an LLM
will confabulate most fluently, and exactly what a reader will trust most.

**Rule: every claim carries its evidence grade, and "I couldn't find out" is a good answer.**

Paste into every research subagent prompt:

> **NEVER invent a citation, username, thread ID, date, price, or quote. If you did not
> fetch it, you do not have it.** Mark every claim **[VERIFIED — <url>]** or
> **[UNVERIFIED — search snippet only]**. No unmarked claims. If a page is bot-blocked,
> say so rather than guessing. If you cannot answer a sub-question, write **NOT ESTABLISHED**
> — that is a perfectly good answer and far more useful than a plausible guess.
> Do not pad. A short honest report beats a long confident one.

### Extra guards that actually caught things
- **Ask for disconfirmation explicitly.** "If the evidence contradicts my hypothesis, say
  so" — this is what surfaced that the user's instinct beat the assistant's.
- **Distinguish abstract-read from full-text-read.** The fabricated numbers all came from
  papers whose *abstracts* were genuinely fetched. Require: "if you only read the abstract,
  you may quote the abstract and nothing else."
- **Separate the price-verification pass from the discovery pass**, and tell the consolidator
  which one wins on conflict.
- **Watch spawn depth.** The fabricating agent was a *nested* subagent (depth 2-3). Its
  parent relayed its claims upward without re-checking. If an agent may spawn children,
  require it to mark which findings are its own vs. a child's.

### When it happens anyway
1. Tell the user immediately and specifically — before they act on it. Name what was faked.
2. Prepend a **FABRICATION AUDIT** block to the notes file: itemized FABRICATED /
   VERIFIED / NOT ESTABLISHED lists, so the file can never be re-read naively later.
3. Rebuild conclusions from the verified subset only, and say what the retraction changed.
4. Do not quietly drop the bad claims — the user may already be acting on them.
