---
name: axctl
description: macOS Accessibility CLI for automating native apps. Use when automating System Settings, Finder, Notes, or any native macOS app. Enables clicking buttons, typing text, reading UI state, and performing actions via the Accessibility API.
allowed-tools: Bash(~/code/axctl/*), Bash(osascript:*)
---

# axctl - macOS Accessibility CLI

A CLI tool for automating macOS native applications via the Accessibility API. Like `pyax` but with interaction capabilities (click, type, actions).

**Location:** `~/.claude/skills/axctl/scripts/axctl`

## When to Use

- **Native macOS apps** - System Settings, Finder, Notes, Calendar, Mail, etc.
- **Apps without APIs** - When there's no programmatic interface
- **Complex UI automation** - Multi-step workflows in native apps
- **Reading app state** - Check checkbox values, read text fields, etc.

**DO NOT use for:**
- Chrome/web browsers (use `/chrome-control` skill instead)
- Apps that have CLI/API interfaces (prefer those)

## Prerequisites

The app must be running and have a window. To open apps:
```bash
open -a "System Settings"
open -a "Finder"
open -a "Notes"
```

## Quick Reference

| Command | Purpose |
|---------|---------|
| `apps` | List running apps with windows |
| `tree` | Dump accessibility tree |
| `search` | Find elements by criteria |
| `click` | Click buttons, checkboxes, etc. |
| `action` | Perform specific actions (AXPress, AXOpen) |
| `type` | Type text into text fields |
| `get` | Get attribute value from element |
| `set` | Set attribute value on element |
| `focus` | Bring app to foreground |
| `wait` | Wait for element to appear |

---

## Commands

### apps - List Running Applications

```bash
~/.claude/skills/axctl/scripts/axctl apps
```

Lists all running applications that have windows. Useful to verify app names.

---

### tree - Dump Accessibility Tree

```bash
# Basic tree (shows AXRole, AXTitle, AXValue)
~/.claude/skills/axctl/scripts/axctl tree "System Settings"

# With ref IDs for interaction
~/.claude/skills/axctl/scripts/axctl tree "System Settings" --refs

# Show available actions on each element
~/.claude/skills/axctl/scripts/axctl tree "System Settings" --list-actions

# All attributes
~/.claude/skills/axctl/scripts/axctl tree "Notes" --all-attributes

# JSON output
~/.claude/skills/axctl/scripts/axctl tree "Finder" --json

# Web area only (for apps with web views)
~/.claude/skills/axctl/scripts/axctl tree "Safari" --web
```

**Tip:** Use `--refs` to get ref IDs, then use those refs in click/action/type commands.

---

### search - Find Elements

```bash
# Find by exact title
~/.claude/skills/axctl/scripts/axctl search "System Settings" --title "Notes"

# Find by role
~/.claude/skills/axctl/scripts/axctl search "System Settings" --role AXButton

# Find by value
~/.claude/skills/axctl/scripts/axctl search "Notes" --value "My note content"

# Find containing text (case-insensitive)
~/.claude/skills/axctl/scripts/axctl search "System Settings" --contains "iCloud"

# Show available actions
~/.claude/skills/axctl/scripts/axctl search "System Settings" --contains "Notes" --list-actions

# JSON output
~/.claude/skills/axctl/scripts/axctl search "Finder" --role AXRow --json
```

The output shows index numbers you can use with `--index` in other commands.

---

### click - Click Elements

```bash
# Click by title
~/.claude/skills/axctl/scripts/axctl click "System Settings" --title "Notes"

# Click by value
~/.claude/skills/axctl/scripts/axctl click "System Settings" --value "iCloud"

# Click by role (first match)
~/.claude/skills/axctl/scripts/axctl click "Finder" --role AXButton

# Click specific match by index
~/.claude/skills/axctl/scripts/axctl click "System Settings" --role AXCheckBox --index 2

# Click by description
~/.claude/skills/axctl/scripts/axctl click "Notes" --desc "New Note"

# Click by ref ID (from tree --refs)
~/.claude/skills/axctl/scripts/axctl click "System Settings" --ref ref_42
```

**How it works:** Tries AXPress first, then AXOpen, then first available action.

---

### action - Perform Specific Actions

```bash
# Press action (same as click for most elements)
~/.claude/skills/axctl/scripts/axctl action "System Settings" AXPress --title "Notes"

# Open action (for rows, files)
~/.claude/skills/axctl/scripts/axctl action "Finder" AXOpen --role AXRow --index 0

# Show default action (for buttons)
~/.claude/skills/axctl/scripts/axctl action "Notes" AXShowDefaultUI --role AXButton --index 0

# Cancel action
~/.claude/skills/axctl/scripts/axctl action "System Settings" AXCancel --role AXSheet
```

**Common actions:**
- `AXPress` - Click/press element
- `AXOpen` - Open file/folder
- `AXShowMenu` - Show context menu
- `AXConfirm` - Confirm dialog
- `AXCancel` - Cancel dialog
- `AXRaise` - Bring to front
- `AXIncrement` / `AXDecrement` - For sliders

---

### type - Type Text

```bash
# Type into first text field
~/.claude/skills/axctl/scripts/axctl type "Notes" "Hello world" --role AXTextArea

# Type into specific field by title
~/.claude/skills/axctl/scripts/axctl type "Safari" "https://example.com" --title "Address and Search"

# Type by ref
~/.claude/skills/axctl/scripts/axctl type "System Settings" "search term" --ref ref_15
```

**Note:** This sets AXValue directly, which works for most text fields. For some apps you may need to use `osascript` keystroke simulation.

---

### get - Get Attribute Value

```bash
# Get value of a checkbox
~/.claude/skills/axctl/scripts/axctl get "System Settings" AXValue --role AXCheckBox --index 0

# Get title of focused element
~/.claude/skills/axctl/scripts/axctl get "Finder" AXTitle --role AXTextField

# Get text content
~/.claude/skills/axctl/scripts/axctl get "Notes" AXValue --role AXTextArea

# Get app's frontmost status
~/.claude/skills/axctl/scripts/axctl get "System Settings" AXFrontmost
```

---

### set - Set Attribute Value

```bash
# Set text value
~/.claude/skills/axctl/scripts/axctl set "Notes" AXValue "New content" --role AXTextArea

# Set checkbox (boolean)
~/.claude/skills/axctl/scripts/axctl set "System Settings" AXValue true --role AXCheckBox --bool

# Set slider (integer)
~/.claude/skills/axctl/scripts/axctl set "System Settings" AXValue 50 --role AXSlider --int
```

---

### focus - Focus Application

```bash
~/.claude/skills/axctl/scripts/axctl focus "System Settings"
~/.claude/skills/axctl/scripts/axctl focus "Finder"
```

Brings the app to the foreground and activates it.

---

### wait - Wait for Element

```bash
# Wait for element with title (default 30s timeout)
~/.claude/skills/axctl/scripts/axctl wait "System Settings" --title "Notes"

# Wait with custom timeout
~/.claude/skills/axctl/scripts/axctl wait "Safari" --contains "Loading" --timeout 60

# Wait for element to appear after navigation
~/.claude/skills/axctl/scripts/axctl wait "System Settings" --role AXCheckBox --timeout 10
```

Useful for automation scripts where UI takes time to load.

---

## Common Workflows

### Enable iCloud Setting

```bash
# Open System Settings
open -a "System Settings"
sleep 2

# Focus it
~/.claude/skills/axctl/scripts/axctl focus "System Settings"

# Navigate to Apple ID
~/.claude/skills/axctl/scripts/axctl click "System Settings" --title "Apple Account"
sleep 1

# Click iCloud
~/.claude/skills/axctl/scripts/axctl click "System Settings" --title "iCloud"
sleep 1

# Find and click Notes
~/.claude/skills/axctl/scripts/axctl click "System Settings" --title "Notes"
```

### Read Note Content

```bash
# Get the text from Notes app
~/.claude/skills/axctl/scripts/axctl get "Notes" AXValue --role AXTextArea
```

### Navigate Finder

```bash
# Open Finder to specific path
open ~/Documents

# Click on a file (first row)
~/.claude/skills/axctl/scripts/axctl click "Finder" --role AXRow --index 0
```

### Check Checkbox State

```bash
# Get checkbox value (0 or 1)
~/.claude/skills/axctl/scripts/axctl get "System Settings" AXValue --role AXCheckBox --title "Sync this Mac"
```

---

## Tips and Gotchas

### 1. App Names Must Match Exactly
The app name must match what appears in the menu bar. Check with `axctl apps`.

### 2. Use search Before click
When unsure about element names, search first:
```bash
~/.claude/skills/axctl/scripts/axctl search "System Settings" --contains "iCloud" --list-actions
```

### 3. Wait for UI to Load
After opening an app or navigating, add a sleep or use `wait`:
```bash
open -a "System Settings"
sleep 2
# or
~/.claude/skills/axctl/scripts/axctl wait "System Settings" --title "Apple Account"
```

### 4. Index for Multiple Matches
When there are multiple matches, use `--index`:
```bash
# Click the 3rd checkbox
~/.claude/skills/axctl/scripts/axctl click "System Settings" --role AXCheckBox --index 2
```

### 5. Some Actions Require Permissions
System Settings changes may trigger password dialogs - those require manual input.

### 6. Checkboxes Use AXValue 0/1
To check state: `get ... AXValue` returns 0 (unchecked) or 1 (checked)
To set state: `set ... AXValue 1 --int` or use `click` to toggle

### 7. Text Fields May Need Focus First
If typing doesn't work, try clicking the field first:
```bash
~/.claude/skills/axctl/scripts/axctl click "App" --role AXTextField
~/.claude/skills/axctl/scripts/axctl type "App" "text" --role AXTextField
```

---

## Common AX Roles

| Role | What It Is |
|------|------------|
| AXButton | Clickable button |
| AXCheckBox | Checkbox (value 0/1) |
| AXTextField | Single-line text input |
| AXTextArea | Multi-line text input |
| AXStaticText | Read-only text label |
| AXRow | Table/list row |
| AXCell | Table cell |
| AXPopUpButton | Dropdown menu |
| AXSlider | Slider control |
| AXRadioButton | Radio button |
| AXTabGroup | Tab container |
| AXTab | Individual tab |
| AXSheet | Modal dialog |
| AXWindow | Application window |
| AXGroup | Container/grouping |
| AXScrollArea | Scrollable region |

---

## Debugging

### See Full Tree
```bash
~/.claude/skills/axctl/scripts/axctl tree "App Name" --all-attributes --list-actions
```

### Find Specific Element
```bash
# Search with multiple criteria
~/.claude/skills/axctl/scripts/axctl search "App" --contains "keyword" --list-actions --json
```

### Check if App is Running
```bash
~/.claude/skills/axctl/scripts/axctl apps | grep -i "app name"
```

---

## Version

```bash
~/.claude/skills/axctl/scripts/axctl version
# axctl 1.0.0
```
