---
name: notes-app
description: Read, write, search, and manage Apple Notes via AppleScript/osascript. Use when asked about notes, creating notes, reading notes, or managing Apple Notes content.
---

# Apple Notes Skill

Read, write, search, and manage Apple Notes programmatically via AppleScript.

> **⚠️ Known blocker (verified 2026-08-19): every call here hangs ~25s+ and
> returns nothing.** `python3.14` has no Automation grant for `com.apple.Notes`,
> so the AppleEvent waits on a prompt that never appears. A denied or pending
> Automation grant **times out rather than erroring**, so this looks exactly like
> a wedged Notes.app — do not debug it by restarting Notes.
>
> Check first (0 = denied, 2 = allowed, missing row = never prompted):
> ```bash
> sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
>   "SELECT client,indirect_object_identifier,auth_value FROM access WHERE service='kTCCServiceAppleEvents';"
> ```
>
> Fix needs a human: System Settings → Privacy & Security → Automation → tick
> **Notes** for python/uv.
>
> **NEVER run `tccutil reset AppleEvents`** to force a re-prompt — it resets the
> whole service, revoking the `MobileSMS` grant that `send-sms` runs on, i.e. you
> would cut the channel you need to report the problem.
> Full writeup: `~/.claude/skills/contacts/SKILL.md` → Troubleshooting.

## Quick Reference

```bash
# List all notes
osascript -e 'tell application "Notes" to get name of every note'

# Read a note by name
osascript -e 'tell application "Notes" to get body of note "My Note"'

# Create a note
osascript -e 'tell application "Notes" to make new note with properties {name:"Title", body:"Content"}'

# Delete a note
osascript -e 'tell application "Notes" to delete note "My Note"'
```

## Commands

### List Notes

```bash
# List all note names
osascript -e 'tell application "Notes" to get name of every note'

# Count notes
osascript -e 'tell application "Notes" to count notes'

# List notes in specific folder
osascript -e 'tell application "Notes" to get name of every note in folder "Work"'

# List all folders
osascript -e 'tell application "Notes" to get name of every folder'
```

### Read Notes

```bash
# Read note body (returns HTML)
osascript -e 'tell application "Notes" to get body of note "My Note"'

# Read note creation date
osascript -e 'tell application "Notes" to get creation date of note "My Note"'

# Read note modification date
osascript -e 'tell application "Notes" to get modification date of note "My Note"'

# Read specific note by index
osascript -e 'tell application "Notes" to get body of note 1'
```

### Create Notes

```bash
# Create simple note
osascript -e 'tell application "Notes" to make new note with properties {name:"Title", body:"Content"}'

# Create note in specific folder
osascript << 'EOF'
tell application "Notes"
    tell folder "Work"
        make new note with properties {name:"Meeting Notes", body:"<h1>Meeting</h1><p>Points...</p>"}
    end tell
end tell
EOF

# Create note with HTML formatting
osascript << 'EOF'
tell application "Notes"
    make new note with properties {name:"Formatted Note", body:"<h1>Header</h1><ul><li>Item 1</li><li>Item 2</li></ul>"}
end tell
EOF
```

### Update Notes

```bash
# Update note body
osascript -e 'tell application "Notes" to set body of note "My Note" to "New content"'

# Append to note
osascript << 'EOF'
tell application "Notes"
    set currentBody to body of note "My Note"
    set body of note "My Note" to currentBody & "<p>Appended content</p>"
end tell
EOF
```

### Delete Notes

```bash
# Delete by name
osascript -e 'tell application "Notes" to delete note "My Note"'

# Delete by index
osascript -e 'tell application "Notes" to delete note 1'
```

### Search Notes

```bash
# Search for notes containing text (requires iteration)
osascript << 'EOF'
tell application "Notes"
    set matchingNotes to {}
    repeat with aNote in every note
        if body of aNote contains "search term" then
            set end of matchingNotes to name of aNote
        end if
    end repeat
    return matchingNotes
end tell
EOF
```

### Working with Folders

```bash
# Create folder
osascript -e 'tell application "Notes" to make new folder with properties {name:"New Folder"}'

# Move note to folder
osascript << 'EOF'
tell application "Notes"
    move note "My Note" to folder "Archive"
end tell
EOF
```

### Working with Accounts

```bash
# List accounts (iCloud, On My Mac, etc.)
osascript -e 'tell application "Notes" to get name of every account'

# Create note in specific account
osascript << 'EOF'
tell application "Notes"
    tell account "iCloud"
        make new note with properties {name:"iCloud Note", body:"Synced everywhere"}
    end tell
end tell
EOF
```

## HTML Formatting

Notes use HTML for formatting. Supported tags:

- `<h1>` to `<h6>` - Headers
- `<p>` - Paragraphs
- `<ul>`, `<ol>`, `<li>` - Lists
- `<b>`, `<strong>` - Bold
- `<i>`, `<em>` - Italic
- `<u>` - Underline
- `<a href="...">` - Links
- `<br>` - Line breaks
- `<div>` - Containers

## Working with Shared Notes

Shared iCloud notes require accepting invitations before they're accessible via AppleScript.

### Accepting Shared Note Invitations

**The "Join Note" button is rendered in a web view** - not accessible via axctl or System Events keyboard navigation. You must use **cliclick with pixel coordinates**.

**Workflow:**

1. **Open Notes app and click Shared folder**
2. **Click on the pending invitation** in the notes list
3. **Find and click the "Join Note" button** using pixel detection (it's orange/gold colored)
4. **After joining**, the note becomes accessible via AppleScript

### Example: Accepting Invitations with Pixel Detection

```bash
# 1. Open Notes and navigate to Shared folder
osascript -e 'tell application "Notes" to activate'
sleep 1

# 2. Click on Shared folder (adjust coords based on window position)
# Get window bounds first:
osascript -e 'tell application "Notes" to get bounds of front window'
# Returns: left, top, right, bottom in logical coords

# 3. Click on the invitation in the list, then find the Join Note button
# Take screenshot and find orange button:
screencapture -x /tmp/notes_screen.png

# 4. Use Python to find the orange "Join Note" button
uv run --with pillow python3 << 'EOF'
from PIL import Image
img = Image.open('/tmp/notes_screen.png')
# Search for orange pixels (R>220, 140<G<200, B<80) in the right pane
orange_pixels = []
for y in range(500, 1500):
    for x in range(2000, 2800):  # Right pane area
        r, g, b = img.getpixel((x, y))[:3]
        if r > 220 and 140 < g < 200 and b < 80:
            orange_pixels.append((x, y))
if orange_pixels:
    center_x = (min(p[0] for p in orange_pixels) + max(p[0] for p in orange_pixels)) // 2
    center_y = (min(p[1] for p in orange_pixels) + max(p[1] for p in orange_pixels)) // 2
    print(f"cliclick c:{center_x//2},{center_y//2}")
EOF

# 5. Click the button with cliclick using the coordinates from above
cliclick c:1214,633  # Example coords - use actual values from detection
sleep 2

# 6. Now the note is accessible
osascript -e 'tell application "Notes" to get name of every note'
```

### Reading Note Content via Accessibility API

After a note is open in Notes, you can read its content via axctl (faster than AppleScript for large notes):

```bash
# Read current note content from the UI
~/dispatch/bin/axctl tree "Notes" 2>&1 | grep -A 5 "AXTextArea"
# Returns: AXTextArea AXValue='Note content here...'
```

### Reading Shared Notes via AppleScript

```bash
# List all notes (including newly accepted shared notes)
osascript -e 'tell application "Notes" to get name of every note'

# Read a shared note
osascript -e 'tell application "Notes" to get body of note "note name"'
```

### SQLite Database Access

Notes are stored in a SQLite database - useful for querying metadata or finding pending invitations:

```bash
# Database location
~/Library/Group\ Containers/group.com.apple.notes/NoteStore.sqlite

# List pending invitations (shared notes not yet accepted)
sqlite3 ~/Library/Group\ Containers/group.com.apple.notes/NoteStore.sqlite "
SELECT ZTITLE, ZSNIPPET, ZSHAREURL FROM ZICINVITATION;
"

# List all note titles
sqlite3 ~/Library/Group\ Containers/group.com.apple.notes/NoteStore.sqlite "
SELECT ZTITLE, ZTITLE1 FROM ZICCLOUDSYNCINGOBJECT
WHERE ZTITLE IS NOT NULL OR ZTITLE1 IS NOT NULL;
"
```

**Note:** The note body (ZDATA column in ZICNOTEDATA) is stored as compressed binary/protobuf, not plain text.

### Important Notes

- **"Join Note" button is NOT accessible** - It's in a web view, so axctl/System Events can't see it
- **Keyboard navigation does NOT work** - Tab+Enter won't reach the button
- **Use pixel detection + cliclick** - Find the orange button by color, click with coordinates
- **Invitations vs. Accepted notes** - The "Shared" folder count includes pending invitations, but AppleScript only sees accepted notes
- **SQLite shows invitation metadata** - ZICINVITATION table has title, snippet, and share URL for pending invitations

## Limitations

1. **Password-protected notes** - Cannot read locked notes
2. **Attachments** - Images/attachments have limited support
3. **Tags** - Can read notes with tags but tags may be stripped from body
4. **Shared notes** - Must be accepted in Notes UI (keyboard navigation) before AppleScript can access them
5. **Performance** - Large operations can be slow via AppleScript

## Python Alternative

For more advanced operations, consider [macnotesapp](https://github.com/RhetTbull/macnotesapp):

```bash
# List notes
uv run --with macnotesapp notes --list

# Get note content
uv run --with macnotesapp notes --get "Note Name"
```

## Tips

- Notes app must have permission to be controlled (grant in System Preferences > Security & Privacy > Privacy > Automation)
- iCloud notes sync automatically after changes
- Use `activate` to bring Notes to foreground: `tell application "Notes" to activate`
- Note names must be unique within a folder
