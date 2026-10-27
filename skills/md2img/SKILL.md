---
name: md2img
description: Convert markdown files to PNG images with GitHub styling. Trigger words - markdown to image, md to png, render markdown, screenshot markdown.
---

# md2img Skill

Convert markdown files to beautiful PNG images with GitHub-style rendering.

## Usage

```bash
~/.claude/skills/md2img/scripts/md2img input.md output.png
# or with options:
~/.claude/skills/md2img/scripts/md2img input.md output.png --width 800 --theme dark
```

## Options

- `--width` / `-w`: Image width in pixels (default: 800)
- `--theme` / `-t`: Theme - `light` or `dark` (default: light)

## Examples

```bash
# Basic render
md2img README.md readme.png

# Dark mode, wider
md2img PLAN.md plan.png -w 1000 -t dark

# From stdin
echo "# Hello" | md2img - output.png
```

## Requirements

- Node.js
- mdimg (`npm install -g mdimg`)

## When to Use

- Rendering plans to send as images in SMS/iMessage
- Creating shareable screenshots of documentation
- Converting markdown to images for social media

## Gotchas

- **Don't use `-t dark` for tables.** The dark theme renders table cell text dark-on-dark, so you can't read it (seen 2026-10-24). Use the default light theme for anything with tables.
