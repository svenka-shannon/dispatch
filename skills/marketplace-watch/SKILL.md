---
name: marketplace-watch
description: Search and watch Facebook Marketplace + Craigslist (SF Bay / Peninsula) for used items near home, and alert on new standout listings. Trigger words - marketplace, craigslist, facebook marketplace, used, secondhand, keep an eye out, watch listings, offerup.
---

# Marketplace Watch

FB Marketplace is browsed logged out through chrome-control (about 45 results per query). Craigslist SF Bay
(peninsula + SF) is static HTML. Both CLIs live in `~/.claude/skills/marketplace-watch/scripts/`.

## Commands
```bash
mpwatch search "coffee table" --max-price 600 [--source fb,cl] [--json]   # one-off; does NOT mark seen
mpwatch add <w> --query "q" --max-price 800 --note "taste notes"          # then: set <w> --fb-queries "a,b,c" --chat <id>
mpwatch list | mpwatch set <w> ... | mpwatch rm <w>
mpwatch new <w> --json > new.json     # main query + every fb_queries entry, deduped, marks seen (--no-mark to peek)
mpwatch new <w> --brief --save new.json [--exclude w,w] [--show-dropped]  # same search, one line per listing:
                                      # index source price title [dims] · location; drops titles matching `exclude`
mpwatch new <w> --brief --from new.json   # re-print a saved list (no search, no marking)
mpwatch set <w> --exclude "glass,round,desk"   # per-watch title filter ('' clears); --thing "a coffee table"
contact-sheet new.json -o /tmp/mpX/sheet.png --imgdir /tmp/mpX/imgs       # numbered photo grids; Read them
mpwatch status <w>                    # days since last reply, phase, sweep today?, check-in state
mpwatch sweep-due <w> | mpwatch checkin-due <w> [--mark]   # exit 0 = yes, 1 = no, 2 = no such watch
mpwatch engaged <w> [--note "keep"]   # someone replied about this watch; resumes daily sweeps
mpwatch detail <url> [<url> ...] [--image-out f.jpg | --image-dir D]       # full description + photo (+ CL price)
mpwatch card <w> --picks picks.json   # detail -> card -> render -> crop -> lastcard push. Prints PNG. Never sends.
mpwatch lastcard find <reply>         # "MMDD-N", "1", "walnut oakland" -> pick + link (runners-up too)
mpwatch lastcard show [--date MMDD] [--considered] | lastcard note <id> "feedback" | lastcard push picks.json
```
`picks.json` (1-3 picks; `considered` = runners-up you judged and didn't send, so later questions can be answered):
```json
{"subtitle": "2 of 57 new listings",
 "picks": [{"url": "...", "price": "$100", "title": "Crate & Barrel Frame, walnut top",
            "meta": "Oakland · ~48 x 28 (est) · FB", "why": "one line: why it fits, what's off", "image": "optional"}],
 "considered": [{"url": "...", "title": "...", "price": "$150", "note": "too small"}]}
```
Missing `title`/`price` come from the listing; `image` (a path or URL) overrides the detail photo.
State: `~/dispatch/state/marketplace-watch.json` (watches, seen ids, `chat`) and
`marketplace-watch-lastcard.json` (v2: `cards` with ids `MMDD-N` + `considered`, 14 days). Edit it only via `mpwatch lastcard`.

## Daily flow (cron spawn, about 10am)
0. `mpwatch sweep-due <w>`: exit 1 means not a sweep day, so stop silently. Then `mpwatch checkin-due <w>`: exit 0
   means nobody has replied for 7 days. Send the printed check-in text to the watch's chat, run
   `mpwatch checkin-due <w> --mark`, and stop (no card today). After that, sweeps run every 3rd day until someone replies.
1. `mpwatch new <w> --brief --save /tmp/mpX/new.json` (a few minutes). Line N is contact-sheet tile N.
   Then `contact-sheet /tmp/mpX/new.json ...` and Read the sheets. Don't hand-write a lister.
2. Judge against the watch `note` (`mpwatch list`). If `--brief` lets obvious junk through, add the word with
   `set <w> --exclude`. Run `mpwatch detail` on every candidate to get the size before shortlisting. Pick 0-3 standouts, not a dump.
3. Write picks.json and run `mpwatch card <w> --picks picks.json`. It warns if a pick was already on an earlier card.
   Read the PNG, then post it (see below). Nothing stands out: send nothing. Optionally `lastcard push` just the runners-up.
4. When someone replies with a number or a description, run `mpwatch lastcard find "<reply>"` and send that link.
   A bare number matches every card's #N, newest first, so confirm if the reply might be about an older card.
   Record reactions with `lastcard note <id> "..."`. Any other reply about the watch (including "keep" after a
   check-in) gets `mpwatch engaged <w> --note "..."`. Both reset the 7-day clock.
5. When they say they bought one, or answer "stop", run `mpwatch rm <w>` and cancel the cron reminder.
5b. "Pause" (not stop): keep the watch (seen ids, note, lastcard). Save the cron record to
   `~/dispatch/state/marketplace-watch-paused-cron.json`, run `claude-assistant remind cancel <id>`, and kill any running
   `<chat>-spawn-<id>` session (`claude-assistant status`). The cron fires at 10am PT, so a pause sent around then races a live sweep.
   Resume: `claude-assistant remind add` with the saved title/cron/`--target spawn`/contact, plus `mpwatch engaged <w>`.
6. End the spawn's summary in plain past tense ("card sent, picks saved"). "I'll…" wording trips the idle-commitment watchdog.

## Gotchas
- Ignore the cron prompt's generic "TEXT the user: working on it" step. Send nothing until there is a card.
- Don't loop ad-hoc `search` calls for the sweep, because they don't mark seen. Add narrow queries with `set --fb-queries`.
- Private-seller value is on FB. Craigslist furniture is mostly $1k+ dealers. Use many narrow FB queries.
- Size decides: round/"Danish teak" FB tables are usually 27-30 in (end-table size). `detail` clicks "See more".
  Dimensions only in photos: `chrome open`, `chrome key <tab> Escape`, page through and screenshot.
  "Estimated (WxDxH)" on FB is an estimate. No size given: estimate from the photo and label it "~" / "est".
- FB pads thin results with far-off cities. Check the location; more than about an hour's drive is out unless exceptional.
- Title and description prices can disagree. Put both on the card. Say "still listed", not "available" (logged out).
- If the detail photo is a phone screenshot, pass `"image": "/tmp/mpX/imgs/<N>.jpg"`. Contact-sheet tile N is
  item N in new.json, counting from 0. Map by URL, never by index.
- Only Craigslist results: look for `[fb] eval failed` on stderr. `search_fb` reopens a Chrome window and retries once.
  `[cl] nodename nor servname`: check `ifconfig en1` for a self-assigned link-local address (IPv4/DHCP lost),
  then escalate to the admin. Unseen CL listings come back on the next good run.
- Read rejects images wider than 2000px. Use `sips --resampleHeightWidthMax 2000`.
- zsh doesn't word-split `$var`. Pass several urls to one `mpwatch detail` call instead of looping.

## Where to post
Post each watch's card to the chat in its `chat` field (`mpwatch list`), never to a 1:1, as `cd ~/transcripts/imessage/<chat>
&& ~/.claude/skills/sms-assistant/scripts/reply "" --image <png>`. That works from a spawn session. `mpwatch card` prints this line.
