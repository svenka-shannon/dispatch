---
name: newspaper
description: Format any content as a newspaper-style HTML page with classic broadsheet layout. Uses Anthropic color theme. Trigger words - newspaper, broadsheet, gazette, dispatch, print layout, editorial.
---

# Newspaper

Generate classic newspaper-style HTML layouts from any content. Parallel to html-reports and visual explainers, but styled as a printed broadsheet.

## Design Language

Inspired by classic broadsheets — multi-column layouts, serif typography, rule lines, masthead, dateline, and editorial hierarchy. Uses **Anthropic brand colors** as the accent palette.

### Anthropic Color Palette

| Use | Color | Hex |
|-----|-------|-----|
| Background | Warm cream | `#FAF7F2` |
| Text | Near black | `#191918` |
| Masthead bg | Dark charcoal | `#1a1a2e` |
| Masthead text | Cream | `#FAF7F2` |
| Accent (primary) | Anthropic terracotta | `#D97757` |
| Accent (secondary) | Deep rust | `#B85C3A` |
| Rule lines | Warm gray | `#C4B5A0` |
| Section heads | Charcoal | `#2D2D2D` |
| Byline/dateline | Muted warm gray | `#8A7E72` |
| Sidebar bg | Light tan | `#F0EBE3` |
| Pull quote border | Terracotta | `#D97757` |

### Typography

- **Masthead:** Georgia, 'Times New Roman', serif — bold, large
- **Headlines:** Georgia, serif — bold, varying sizes by importance
- **Body:** 'Source Serif Pro', Georgia, serif — 13px, line-height 1.5
- **Bylines/dateline:** -apple-system, sans-serif — small, muted, uppercase
- **Section labels:** Sans-serif, uppercase, letter-spaced, small

## Template Structure

The newspaper layout has these zones:

```
+------------------------------------------+
|              MASTHEAD                     |
|   Title . Subtitle . Date . Edition      |
+------------------------------------------+
| TICKER BAR (optional one-line updates)   |
+---------------------+--------------------+
|                     |                    |
|  MAIN HEADLINE      |   SIDEBAR          |
|  (2-col span)       |   (1 col)          |
|                     |                    |
|  Lead story body    |   Short items      |
|  text...            |   with borders     |
|                     |                    |
+---------------------+--------------------+
|  SECONDARY STORIES (2-3 column grid)     |
|  +------+ +------+ +------+             |
|  | Story| |Story | |Story |             |
|  +------+ +------+ +------+             |
+------------------------------------------+
|  EDITOR'S DESK (full width, boxed)       |
|  Commentary, updates, notes              |
+------------------------------------------+
```

## Usage

Generate newspaper HTML by writing content to the template. The skill provides the CSS and layout structure — you populate it with content.

### Quick Start

1. Write an HTML file using the base template below
2. Fill in the content sections
3. Send via iMessage or publish via sven-pages

### Generating via Script

```bash
# Generate newspaper from a JSON content file
~/.claude/skills/newspaper/scripts/generate --title "The Dispatch" --subtitle "All the news fit to commit" --content content.json -o /tmp/newspaper.html
```

### Sending

```bash
# Via iMessage as file
~/.claude/skills/sms-assistant/scripts/send-sms "chat_id" --file /tmp/newspaper.html

# Via sven-pages (permanent URL, private by default)
~/.claude/skills/sven-pages/scripts/publish /tmp/newspaper-folder --acl "participant@gmail.com"

# Only if explicitly asked to make it public:
~/.claude/skills/sven-pages/scripts/publish /tmp/newspaper-folder --public
```

## Base Template

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,wght@0,400;0,600;0,700;1,400&display=swap');

*{margin:0;padding:0;box-sizing:border-box}

body{
  font-family:'Source Serif 4',Georgia,'Times New Roman',serif;
  background:#FAF7F2;color:#191918;
  max-width:1100px;margin:0 auto;padding:0;
  line-height:1.5;font-size:13px;
}

/* Masthead */
.masthead{
  background:#1a1a2e;color:#FAF7F2;
  text-align:center;padding:16px 20px 12px;
  border-bottom:4px double #D97757;
}
.masthead h1{
  font-family:Georgia,'Times New Roman',serif;
  font-size:42px;font-weight:700;letter-spacing:2px;
  margin:0;line-height:1.1;
}
.masthead .subtitle{
  font-family:-apple-system,sans-serif;
  font-size:11px;color:#8A7E72;
  letter-spacing:3px;text-transform:uppercase;
  margin-top:4px;
}
.masthead .dateline{
  font-family:-apple-system,sans-serif;
  font-size:10px;color:#8A7E72;
  margin-top:6px;display:flex;justify-content:space-between;
  border-top:1px solid #3a3a4e;padding-top:6px;
}

/* Ticker */
.ticker{
  background:#F0EBE3;border-bottom:1px solid #C4B5A0;
  padding:6px 20px;font-size:11px;color:#8A7E72;
  font-family:-apple-system,sans-serif;
  display:flex;gap:20px;overflow:hidden;
}
.ticker strong{color:#D97757}

/* Main content area */
.content{padding:16px 20px}

/* Main story grid */
.main-grid{
  display:grid;
  grid-template-columns:2fr 1fr;
  gap:0;
  border-bottom:2px solid #191918;
  padding-bottom:16px;margin-bottom:16px;
}
.main-story{
  padding-right:20px;
  border-right:1px solid #C4B5A0;
}
.sidebar{padding-left:20px}

/* Headlines */
.headline-main{
  font-size:32px;font-weight:700;line-height:1.15;
  margin-bottom:4px;
}
.headline-sub{
  font-size:20px;font-weight:700;line-height:1.2;
  margin-bottom:4px;
}
.headline-minor{
  font-size:16px;font-weight:700;line-height:1.2;
  margin-bottom:4px;
}
.kicker{
  font-family:-apple-system,sans-serif;
  font-size:10px;text-transform:uppercase;
  letter-spacing:1.5px;color:#D97757;
  font-weight:600;margin-bottom:2px;
}
.byline{
  font-family:-apple-system,sans-serif;
  font-size:10px;color:#8A7E72;
  text-transform:uppercase;letter-spacing:0.5px;
  margin:4px 0 8px;
}

/* Body text */
.body-text{
  font-size:13px;line-height:1.55;
  column-count:2;column-gap:20px;
  column-rule:1px solid #C4B5A0;
}
.body-text.single-col{column-count:1}
.body-text p{margin-bottom:8px;text-align:justify}
.drop-cap::first-letter{
  float:left;font-size:48px;line-height:36px;
  padding:2px 6px 0 0;font-weight:700;color:#D97757;
}

/* Pull quote */
.pull-quote{
  border-left:3px solid #D97757;
  padding:8px 12px;margin:12px 0;
  font-size:16px;font-style:italic;
  color:#2D2D2D;line-height:1.4;
}

/* Sidebar items */
.sidebar-item{
  border-bottom:1px solid #C4B5A0;
  padding-bottom:10px;margin-bottom:10px;
}
.sidebar-item:last-child{border-bottom:none}

/* Secondary stories grid */
.secondary-grid{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:0;
  border-bottom:2px solid #191918;
  padding-bottom:16px;margin-bottom:16px;
}
.secondary-story{
  padding:0 16px;
  border-right:1px solid #C4B5A0;
}
.secondary-story:first-child{padding-left:0}
.secondary-story:last-child{border-right:none;padding-right:0}

/* Editor's desk */
.editors-desk{
  background:#F0EBE3;
  border:1px solid #C4B5A0;
  padding:12px 16px;margin-top:8px;
}
.editors-desk .desk-title{
  font-family:-apple-system,sans-serif;
  font-size:10px;text-transform:uppercase;
  letter-spacing:2px;color:#D97757;
  font-weight:700;margin-bottom:8px;
  border-bottom:1px solid #C4B5A0;
  padding-bottom:4px;
}
.editors-desk .entry{
  margin-bottom:8px;font-size:12px;
}
.editors-desk .entry-label{
  font-family:-apple-system,sans-serif;
  font-size:9px;color:#8A7E72;
  text-transform:uppercase;letter-spacing:0.5px;
}

/* Rule lines */
.rule-thick{border-top:2px solid #191918;margin:12px 0}
.rule-thin{border-top:1px solid #C4B5A0;margin:8px 0}
.rule-double{border-top:4px double #191918;margin:12px 0}

/* Footer */
.footer{
  text-align:center;padding:8px;
  font-family:-apple-system,sans-serif;
  font-size:9px;color:#8A7E72;
  border-top:1px solid #C4B5A0;
  margin-top:16px;
}
</style>
</head>
<body>

<!-- MASTHEAD -->
<div class="masthead">
  <h1>{{TITLE}}</h1>
  <div class="subtitle">{{SUBTITLE}}</div>
  <div class="dateline">
    <span>{{EDITION}}</span>
    <span>{{DATE}}</span>
    <span>{{TAGLINE}}</span>
  </div>
</div>

<!-- TICKER (optional) -->
<div class="ticker">
  <span><strong>BREAKING:</strong> Ticker item 1</span>
  <span><strong>UPDATE:</strong> Ticker item 2</span>
</div>

<div class="content">

<!-- MAIN STORY + SIDEBAR -->
<div class="main-grid">
  <div class="main-story">
    <div class="kicker">SECTION LABEL</div>
    <h2 class="headline-main">Main Headline Goes Here</h2>
    <div class="byline">By Author Name . March 20, 2026</div>
    <div class="body-text">
      <p class="drop-cap">Lead paragraph text...</p>
      <p>Continuation text...</p>
    </div>
  </div>
  <div class="sidebar">
    <div class="sidebar-item">
      <h3 class="headline-minor">Sidebar Story 1</h3>
      <div class="byline">By Author</div>
      <p>Short sidebar text...</p>
    </div>
    <div class="sidebar-item">
      <h3 class="headline-minor">Sidebar Story 2</h3>
      <p>Another short item...</p>
    </div>
  </div>
</div>

<!-- SECONDARY STORIES -->
<div class="secondary-grid">
  <div class="secondary-story">
    <div class="kicker">CATEGORY</div>
    <h3 class="headline-sub">Secondary Headline</h3>
    <div class="byline">By Author</div>
    <p>Story text...</p>
  </div>
  <div class="secondary-story">
    <h3 class="headline-sub">Another Story</h3>
    <p>Story text...</p>
  </div>
  <div class="secondary-story">
    <h3 class="headline-sub">Third Story</h3>
    <p>Story text...</p>
  </div>
</div>

<!-- EDITOR'S DESK -->
<div class="editors-desk">
  <div class="desk-title">Editor's Desk</div>
  <div class="entry">
    <div class="entry-label">FROM THE DESK OF THE EDITOR</div>
    <p>Commentary or notes here...</p>
  </div>
</div>

</div><!-- .content -->

<div class="footer">
  Published {{DATE}} . Generated with Dispatch
</div>

</body>
</html>
```

## Content Guidelines

### Headline Hierarchy

1. **`.headline-main`** (32px) — One per page, the lead story
2. **`.headline-sub`** (20px) — Secondary stories
3. **`.headline-minor`** (16px) — Sidebar items, briefs

### Kickers

Use `.kicker` above headlines for section labels:
```html
<div class="kicker">BREAKING NEWS</div>
<h2 class="headline-main">Headline Here</h2>
```

### Drop Caps

First paragraph of the lead story should use `.drop-cap`:
```html
<p class="drop-cap">The story begins here with a large first letter...</p>
```

### Pull Quotes

For emphasis within articles:
```html
<div class="pull-quote">"This is a notable quote that deserves attention."</div>
```

### Column Text

- Main story body uses 2-column layout by default (`.body-text`)
- Use `.body-text.single-col` for sidebar/secondary stories
- Text is justified for newspaper feel

## Adapting Content

The newspaper skill can format **any** content as a broadsheet:

- **Technical reports** -> Lead story with findings, sidebar with metrics
- **Project updates** -> Headline with status, secondary stories per workstream
- **Daily briefings** -> Ticker for quick updates, stories for details
- **Research summaries** -> Main article with analysis, sidebar for methodology
- **Event recaps** -> Timeline as stories, quotes as pull-quotes

## Content Privacy Rules

**HARD RULE — never reference any of the following in newspaper-generated pages:**
- PassThePopcorn (PTP)
- BTN
- Seedboxes
- Torrents or torrent-related terminology

This applies to **all content types** — headlines, body text, sidebar items, ticker, editor's desk, pull quotes, and any other section. No exceptions.

## Notes

- Template is fully hermetic (inline CSS, Google Fonts import)
- Optimized for 1100px width but responsive
- Print-friendly — light background, dark text, serif fonts
- The reference design is "The Foreman Dispatch" — a project status newspaper

## The Svenka Times (nightly gazette on this box)

The nightly `nightly-sventimes` task (2:40am ET) writes a system-activity gazette using this design language. Pipeline lives in `~/code/svenka-times` (see its README): `gather-daily-data` → `archive-edition` → fill `template.html` → `verify-opsec` → `deploy.sh` (public svenka-pages folder `times`). Upstream's version targets `~/code/svenflow.ai`, which is a private repo not available here, so don't point the task back at it.
