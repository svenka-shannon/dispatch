---
name: chp-traffic
description: Look up California Highway Patrol (CHP) live traffic incidents — accidents, crashes, collisions, hazards, closures — by region, highway or town, plus today's history. Trigger words - CHP, accident, crash, collision, traffic incident, highway closed, sigalert, Hwy 1, what happened on the freeway.
---

# CHP Traffic Incidents

## Live / last hour: `chp-traffic` CLI

```bash
~/.claude/skills/chp-traffic/scripts/chp-traffic                    # Golden Gate (Bay Area) active incidents
~/.claude/skills/chp-traffic/scripts/chp-traffic --filed            # + recently closed (~last hour only)
~/.claude/skills/chp-traffic/scripts/chp-traffic --filed --grep 'sr1\b|pacifica|half moon'
~/.claude/skills/chp-traffic/scripts/chp-traffic --center LACC --json
~/.claude/skills/chp-traffic/scripts/chp-traffic --list             # comm center codes
```

It scrapes cad.chp.ca.gov/Traffic.aspx with ASP.NET postbacks, so no browser is needed. Times are **Pacific**. GGCC (Golden Gate) covers the Bay Area, including San Mateo Coast/Pacifica (area "Redwood City").

## Earlier today / yesterday (history)

CHP's own site **only keeps ~1 hour of closed ("filed") incidents**. For anything older, use CommuteDash, which archives CHP incidents by route:
- `https://commutedash.com/California/SR-1`: open it with chrome-control, then `click-by-name <tab> "What happened on SR-1 today?"` and run `text`. It shows all of today's incidents with times and IDs. A "yesterday" tab is also available.
- Swap the route in the URL: `US-101`, `I-280`, `SR-92`, etc.

## Gotchas
- `https://media.chp.ca.gov/sa_xml/sa.xml` (the "statewide XML feed") was **stale and truncated** (frozen at Oct 23, cut at 128KB) on 2026-10-24. Don't trust it.
- chp.ca.gov/traffic is just an info page. The live data is at cad.chp.ca.gov.
- CHP only logs state highways and unincorporated roads. City-street crashes (e.g. inside Pacifica off Hwy 1) are handled by local PD and won't appear.
