---
name: shopping-list
description: Household shopping list with staples and recipe import (only shown on request, never pushed). Use when anyone says "add X to the list", "we need X", "what's on the shopping list", "got the X", "remove X", or asks for the weekly list. Trigger words - shopping list, grocery list, add to list, we're out of, need to buy, staples.
---

# shopping-list

One shared list, grouped by store section, with recurring **staples** that come back every week.

CLI: `~/.claude/skills/shopping-list/scripts/shoplist` (state: `~/dispatch/state/shopping-list.json`)

```bash
S=~/.claude/skills/shopping-list/scripts/shoplist
$S add "eggs" "oat milk" --note "the big one"   # add (dupes are merged)
$S add "coffee beans" --staple                 # add AND make it a weekly staple
$S got eggs milk                               # mark bought (fuzzy match)
$S rm "oat milk"                               # remove
$S text                                        # SMS-ready checklist, grouped by section
$S list --all                                  # include bought items
$S staples | $S staples add X | $S staples rm X
$S week                                        # rollover: drop bought items, re-add staples
```

## How to handle messages
- "add X / we need X / out of X" → `add`, then a 👍 tapback. No text reply needed.
- "what's on the list" → `text`, send the output as-is.
- "got X" / "bought everything" → `got ...` for each item. Confirm with a tapback.
- "always get X" / "every week" → `add X --staple`.
- Several items in one message: split on commas / "and" / newlines and pass them all to one `add`.
- Ambiguous `rm`/`got` prints the candidates. Ask which one; never guess.

## Recipes → list
Eric can send a recipe (link, photo, or pasted text). Extract the ingredient lines, drop quantities
you can't buy separately, then:
```bash
$S add --recipe "Lamb skewers" "lamb shoulder, 2 lb" "cumin seeds" "chili flakes" ...
```
Recipe mode skips anything matching the `pantry` list (salt, pepper, oils, sugar, flour, water) and
notes "for <recipe>" on each item. Reply with ONE line: what was added + what was skipped.
Links: use `webfetch`; NYT Cooking is paywalled, so use Chrome. Photos: read the image.
`$S pantry add X` / `pantry rm X` to adjust what counts as always-in-the-house.

## No scheduled sends (Eric, 2026-09-28)
"Don't send the list every Saturday, just keep it until I demand to see it."
Never push the list unprompted. Only send `shoplist text` when asked.
When he says he's done shopping ("got everything", "shopping done"): mark items `got`, then run
`shoplist week` (drops bought items, re-adds staples for next time).
If he says "order it": hand the open items to the `cooking` skill's Instacart flow (its store is
still `roche-bros`, a Boston store; pick a local store first).
