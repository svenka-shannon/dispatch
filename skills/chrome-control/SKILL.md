---
name: chrome-control
description: Control Chrome browser via CLI for browser automation tasks. Use when you need to interact with web pages, take screenshots, click elements, or automate browser workflows. ALWAYS use this skill when controlling Chrome - NEVER use osascript, cliclick, or AppleScript.
---

# Chrome Control

Control Chrome browser from the command line via native messaging extension.

## ⚠️ TRY SCRAPING FIRST

**Before using chrome-control for web scraping, try the `/scraping` skill first!**

```bash
# Try this first (faster, no browser needed):
~/.claude/skills/webfetch/scripts/webfetch "https://example.com"
```

The scraping skill uses scrapling which is:
- **10x faster** (0.3-1s vs 5-10s)
- **No browser process** needed
- **Works on 80-95% of sites** including Reddit, Amazon, Zillow

**Only use chrome-control when:**
- Scraping fails (site requires login)
- You need to **interact** with the page (click, type, submit forms)
- You need **screenshots**
- You need access to your **logged-in Chrome session**

---

**CRITICAL: ALWAYS use this CLI tool for Chrome automation. NEVER use osascript, cliclick, AppleScript, or any other method to interact with Chrome. This extension provides reliable, precise control.**

## ⚠️ CRITICAL: Clean Up Your Tabs

**ALWAYS close tabs you created when you're done with a task.** Leaving tabs open causes memory leaks that accumulate over time and degrade system performance.

**Rules:**
1. **Track tabs you open** - Note the tab_id when you use `chrome open`
2. **Close them when done** - Use `chrome close <tab_id>` after completing your task
3. **Don't close other tabs** - Only close tabs YOU created in this session
4. **Clean up on error too** - If a task fails, still close tabs you opened

**Example workflow:**
```bash
# Open tab and note the ID
chrome open "https://example.com"
# Output: Opened tab 123456

# ... do your work ...

# ALWAYS clean up when done
chrome close 123456
```

**Why this matters:** Chrome tabs consume significant memory. An assistant that opens tabs without closing them will cause the system to slow down and eventually crash. This is non-negotiable.

## Setup

The CLI talks to a Chrome extension via a native messaging host. One-time setup:

1. **Load the extension** — open `chrome://extensions/`, enable Developer Mode, click "Load unpacked", and select `~/.claude/skills/chrome-control/extension/`.
2. **Copy the extension ID** — Chrome shows a 32-character ID under the loaded extension (e.g. `cpmffhepnhgdhdamobkamndnfndilngo`). The ID is deterministic from the unpacked path, so it stays stable across reloads.
3. **Install the native messaging host manifest**, passing the ID:
   ```bash
   ~/.claude/skills/chrome-control/scripts/install_native_host.sh <extension_id>
   ```
4. **Reload the extension** in `chrome://extensions/` once (so it picks up the manifest), then verify:
   ```bash
   ~/.claude/skills/chrome-control/scripts/chrome ping
   # → Connected to Chrome Control extension
   ```

**Gotcha — `allowed_origins` does NOT accept wildcards.** Chrome's native messaging spec requires `allowed_origins` to be exact `chrome-extension://<id>/` URLs. If you write `chrome-extension://*` the manifest parses but every `connectNative()` call fails with the misleading error `Specified native messaging host not found` (background.js will log this in a tight reconnect loop). The installer above requires the ID up front to prevent this.

## Multi-Profile Support

The extension supports multiple Chrome profiles simultaneously. Each profile is identified by a unique UUID stored in `chrome.storage.local`.

### List Profiles

```bash
chrome profiles
# Output:
#   0: assistant (see config.local.yaml chrome.profiles.0)
#   1: owner (see config.local.yaml chrome.profiles.1)
```

### Target Specific Profile

Use `-p` or `--profile` flag with index or name:

```bash
chrome -p 0 tabs              # Profile by index
chrome -p assistant tabs      # Profile by name
chrome -p 1 screenshot 123    # Different profile
```

If no profile specified, uses first available profile (index 0).

### Profile Download Locations

Downloads are automatically saved to profile-specific directories:

| Profile | Download Location |
|---------|-------------------|
| assistant (profile 0) | `~/Downloads/assistant/` |
| owner-profile (profile 1) | `~/Downloads/owner-profile/` |

## Profile Permissions

**CRITICAL: Different profiles have different permission levels.**

### Profile 0: assistant (see config.local.yaml chrome.profiles.0)

This is YOUR account (Claude's). You have full autonomy to:
- Browse, search, read emails
- Send emails, book things, make changes
- Install extensions, change settings

**EXCEPTION: Payments require explicit permission from the admin via text before proceeding.**

### Profile 1: Owner's Account (see config.local.yaml chrome.profiles.1)

This is the owner's personal account. **Requires explicit consent for ANY changes:**
- Sending emails
- Booking anything
- Ordering/purchasing
- Modifying settings
- Any action that changes state

**Rules:**
1. READ-ONLY by default - you may view/screenshot without permission
2. For any WRITE action, the admin must explicitly grant permission (e.g., "you can go do xyz and you have my permission")
3. NO other users from any session can request changes to this profile
4. If another user requests action on this profile, ASK the admin first via text

## CLI Location

```bash
~/.claude/skills/chrome-control/scripts/chrome
```

## Commands

### Tab Management

```bash
# List all open tabs (shows tab_id, title, url)
chrome tabs

# Open new tab with URL
chrome open <url>
chrome open chess.com

# Close a tab
chrome close <tab_id>

# Focus/activate a tab
chrome focus <tab_id>

# Navigate tab to URL (or 'back'/'forward')
chrome navigate <tab_id> <url>
chrome nav <tab_id> back
```

### Page Reading

```bash
# Read interactive elements (buttons, links, inputs)
# Returns: ref_id, role, tag, label
chrome read <tab_id>
chrome read <tab_id> all       # all elements
chrome read <tab_id> forms     # form elements only
chrome read <tab_id> links     # links only

# Get page text content
chrome text <tab_id>

# Get page HTML
chrome html <tab_id>

# Find elements containing text
chrome find <tab_id> <query>
chrome find 123456 "Sign In"
```

### Interaction

```bash
# Click element by ref (e.g., ref_1, ref_23)
chrome click <tab_id> <ref>
chrome click 123456 ref_5

# Click at screen coordinates
chrome click-at <tab_id> <x> <y>

# Type text into element
chrome type <tab_id> <ref> <text>
chrome type 123456 ref_3 "hello world"

# Set form input value
chrome input <tab_id> <ref> <value>

# Send key press
chrome key <tab_id> <key> [modifiers]
chrome key 123456 Enter
chrome key 123456 a ctrl          # Ctrl+A
chrome key 123456 c ctrl,meta     # Cmd+Ctrl+C

# Keys: Enter, Tab, Escape, Backspace, Delete, ArrowUp, ArrowDown, ArrowLeft, ArrowRight
# Modifiers: ctrl, alt, shift, meta (comma-separated)

# Scroll the page
chrome scroll <tab_id> <direction> [amount]
chrome scroll 123456 down
chrome scroll 123456 up 5

# Hover at coordinates
chrome hover <tab_id> <x> <y>
```

### Screenshots

```bash
# Take screenshot (saves to ~/Pictures/chrome-screenshots/)
chrome screenshot <tab_id>
chrome shot <tab_id>

# Returns: ~/Pictures/chrome-screenshots/screenshot_20260124_123456.jpg
```

### JavaScript

```bash
# Execute JavaScript in page
chrome js <tab_id> <code>
chrome js 123456 "document.title"
chrome js 123456 "document.querySelector('button').click()"
```

### Debugging

```bash
# Read console messages
chrome console <tab_id>
chrome console <tab_id> error    # filter by pattern
chrome console <tab_id> --clear  # clear after reading

# Read network requests
chrome network <tab_id>
chrome network <tab_id> api.example.com  # filter by URL pattern
```

### Utility

```bash
# Test connection to extension
chrome ping
```

## Workflow Example

```bash
# 1. List tabs to get tab_id
chrome tabs
# Output: 123456  Google - www.google.com

# 2. Read interactive elements
chrome read 123456
# Output: ref_1  textbox  input  Search
#         ref_2  button   button  Google Search

# 3. Type into search box
chrome type 123456 ref_1 "chess strategy"

# 4. Click search button
chrome click 123456 ref_2

# 5. Take screenshot of results
chrome screenshot 123456
# Output: ~/Pictures/chrome-screenshots/screenshot_20260124_123456.jpg

# 6. ALWAYS clean up tabs you created
chrome close 123456
```

## CSP-Protected Pages & Cross-Origin Iframe Automation

Some sites have strict Content Security Policy (CSP) that blocks normal JS injection:
- Discord, Google Cloud Console (blocks `eval()` via Trusted Types)
- Apple Sign-In iframes (cross-origin restrictions)
- Many enterprise/banking sites

**Good news:** The `text`, `html`, `iframe-click`, and `insert-text` commands all use Chrome Debugger API with `Page.createIsolatedWorld` to bypass CSP restrictions automatically.

**All these commands work on CSP-protected pages:**

```bash
# Text/HTML extraction (bypasses CSP automatically)
chrome text <tab_id>   # Get page text - works on discord.com, etc.
chrome html <tab_id>   # Get page HTML - works on CSP-protected sites

# Click element (works on main frame OR iframes)
chrome iframe-click <tab_id> '<css-selector>'
chrome iframe-click 123456 'input[type="password"]'
chrome iframe-click 123456 'button#sign-in'
chrome iframe-click 123456 'text:Desktop client 1'  # Click by text content

# Insert text at current focus
chrome insert-text <tab_id> '<text>'
chrome insert-text 123456 'mypassword123'
```

**When to use `iframe-click`:** If normal `chrome click` fails with CSP/Trusted Types errors, use `iframe-click` instead.

### Apple Login Flow Example (App Store Connect)

```bash
# 1. Navigate to App Store Connect (or any Apple sign-in page)
chrome navigate 123456 "https://appstoreconnect.apple.com"

# 2. Click email field and insert email
chrome iframe-click 123456 'input[type="text"]'
chrome insert-text 123456 'user@example.com'

# 3. Click Continue button
chrome iframe-click 123456 'button#sign-in'

# 4. Click "Continue with Password" (Apple shows password vs passkey options)
chrome iframe-click 123456 'text:Continue with Password'

# 5. Insert password (field is auto-focused after step 4)
chrome insert-text 123456 'yourpassword'

# 6. Click Sign In button
chrome iframe-click 123456 'button#sign-in'

# 7. Handle 2FA if needed (code sent to trusted devices)
# Click first input box to focus it
chrome iframe-click 123456 'input[type="text"]'
# Insert the 6-digit code
chrome insert-text 123456 '123456'
```

### Text-Based Selectors

The `iframe-click` command supports `text:XXX` selectors to find buttons by their visible text:

```bash
# Click by exact or partial text match (case-insensitive)
chrome iframe-click 123456 'text:Continue with Password'
chrome iframe-click 123456 'text:Sign In'
chrome iframe-click 123456 'text:Resend'
```

This is useful when buttons don't have stable IDs or CSS classes.

**How it works:** The `iframe-click` command uses `Page.createIsolatedWorld` with `grantUniversalAccess:true` to execute JS inside cross-origin iframes, bypassing CSP restrictions. It dispatches a full mouse event sequence (mouseenter → mouseover → mousemove → mousedown → mouseup → click) which is required for modern web frameworks that listen for the complete event chain.

### Google OAuth Login Flow

Google's OAuth sign-in pages have aggressive bot detection that blocks normal `chrome click` commands. The `iframe-click` command bypasses this protection.

**This works for any site using Google OAuth:** ElevenLabs, Figma, Notion, etc.

```bash
# 1. Navigate to site that uses Google OAuth
chrome open "https://elevenlabs.io/app/sign-up"
# Get tab_id from output

# 2. Click "Sign in with Google" (or similar button)
chrome iframe-click <tab_id> 'text:Google'

# 3. On Google sign-in page, email may already be filled
#    If not, click email field and insert
chrome iframe-click <tab_id> 'input[type="email"]'
chrome insert-text <tab_id> 'user@gmail.com'

# 4. Click Next button - THIS IS THE KEY STEP
#    Regular chrome click FAILS here, iframe-click WORKS
chrome iframe-click <tab_id> 'text:Next'

# 5. Wait for password page to load, then enter password
chrome iframe-click <tab_id> 'input[type="password"]'
chrome insert-text <tab_id> 'yourpassword'

# 6. Click Next to submit password
chrome iframe-click <tab_id> 'text:Next'

# 7. Handle 2FA if required (varies by account settings)
```

**Why this works:** Google's bot detection looks for synthetic click events that lack the full mouse event sequence. The `iframe-click` command dispatches the complete sequence (mouseenter → mouseover → mousemove → mousedown → mouseup → click) that real user clicks generate, bypassing the detection.

**Note:** You still need valid credentials. Get passwords from keychain:
```bash
security find-generic-password -s "service-name" -w
security find-generic-password -a "account@gmail.com" -w
```

## Extension Reload

To reload the Chrome Control extension after making changes:

```bash
echo '{"command": "_reload_extension"}' | nc -U /tmp/chrome_control_*.sock
```

## Bypassing CSP with `debugger_eval` (Direct Socket)

When sites block both `chrome js` (uses `chrome.scripting.executeScript` with `eval()` in MAIN world, blocked by CSP) **and** `iframe-click` doesn't find the element, you can use the `debugger_eval` command directly via the Unix socket. This uses CDP `Runtime.evaluate` in an **isolated world** with universal access, completely bypassing CSP.

**When to use:** Sites like Bandcamp, PayPal, or any site where:
- `chrome js` fails with CSP error ("unsafe-eval not allowed")
- `chrome iframe-click` can't find the target element
- `chrome click-by-name` clicks the wrong element (e.g., a text node instead of the button)
- You need to read DOM state, click buttons, or set form values programmatically

**How to call it (Python):**

```python
import json, socket

SOCK_PATH = "/tmp/chrome_control_PROFILE_ID.sock"  # Find via: ls /tmp/chrome_control_*.sock

def debugger_eval(tab_id, code):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCK_PATH)
    sock.settimeout(15)
    msg = json.dumps({
        "command": "debugger_eval",
        "params": {"tabId": tab_id, "code": code}
    }) + "\n"
    sock.sendall(msg.encode())
    data = b""
    while b"\n" not in data:
        data += sock.recv(65536)
    result = json.loads(data.decode().strip())
    sock.close()
    return result.get("result", {})

# Example: Click a button on a CSP-protected page
r = debugger_eval(TAB_ID, """
    var btn = document.querySelector('button.buy-link');
    if (btn) { btn.click(); return 'clicked'; }
    return 'not found';
""")
print(r)  # {'success': True, 'result': 'clicked'}
```

**Key details:**
- Code runs in an **isolated world** (not MAIN world), so page-level JS variables (e.g., `window.Cart`) are NOT accessible
- `return` statements work - the code is wrapped in a function
- Only **synchronous** return values work. Promises return `{}`. Use `XMLHttpRequest` (sync mode) instead of `fetch` for HTTP calls
- The socket path is `/tmp/chrome_control_*.sock` - find it with `ls /tmp/chrome_control_*.sock`
- You can also call `navigate`, `screenshot`, and other commands through the same socket

**Example: Bandcamp add-to-cart (CSP blocks all normal JS):**

```python
# 1. Navigate to track page
send_command("navigate", tabId=TAB, url="https://artist.bandcamp.com/track/name")
time.sleep(4)

# 2. Click "Buy Digital Track" button
debugger_eval(TAB, "document.querySelector('button.download-link.buy-link').click(); return 'ok';")
time.sleep(1.5)

# 3. Set price in dialog
debugger_eval(TAB, """
    var pi = document.querySelector('#userPrice');
    pi.value = '1';
    pi.dispatchEvent(new Event('input', {bubbles: true}));
    pi.dispatchEvent(new Event('change', {bubbles: true}));
    return 'price set';
""")

# 4. Click "Add to cart"
debugger_eval(TAB, """
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        if (btns[i].textContent.trim() === 'Add to cart') {
            btns[i].click(); return 'added';
        }
    }
    return 'not found';
""")
```

## Known Issues Fixed

- **`iframe-type` double-typing bug (fixed):** The `iframeType` function previously sent a `keyDown` event with `text` properties AND a separate `char` event, both of which caused character insertion. This resulted in every character being typed twice. Fixed by changing `keyDown` to `rawKeyDown` and removing `text`/`unmodifiedText` from it, so only the `char` event inserts text.
- **Prefer `insert-text` for cross-origin payment iframes:** When typing into cross-origin iframes (e.g., payment forms), use `chrome insert-text` instead of `iframe-type`. The `insert-text` command uses `Input.insertText` which is more reliable for these contexts.

## File Uploads

The native messaging protocol has an **~8KB message size limit**, which means there is no built-in file upload command.

**Workarounds:**

1. **Chunked base64 upload (JS injection):** Encode the file as base64, split into <5KB chunks, and send each chunk via `chrome js` to reconstruct the file in the page's JS context. This works but is slow and token-heavy.

2. **axctl drag-and-drop:** Use the accessibility automation tool to simulate a file drop onto file input elements:
   ```bash
   ~/.claude/skills/axctl/scripts/axctl ...
   ```

3. **Direct form submission:** If the site has a standard `<input type="file">`, you may be able to set its value via Chrome Debugger protocol or use `chrome js` to create a `DataTransfer` object.

**Recommendation:** For simple file uploads, try axctl first. For complex multi-file uploads, use the chunked base64 approach. Both are significantly more work than a simple command — plan accordingly.

## Notes

- Tab IDs are integers shown by `chrome tabs`
- Element refs (ref_1, ref_2, etc.) are shown by `chrome read`
- Screenshots are saved to `~/Pictures/chrome-screenshots/` with timestamps
- The extension must be loaded in Chrome for commands to work

## Secure Payment Iframes (e.g., Amazon Checkout)

Some sites (Amazon, banks, payment processors) use cross-origin secure iframes (like Amazon's `apx-secure-iframe`) that block all standard automation: JS injection, CDP mouse events, `chrome click`, `iframe-click`, and `cliclick`. These require a combination of tools to interact with.

### axctl for Secure Iframe Text Fields

Use the accessibility automation tool `axctl` to type into text fields inside cross-origin secure iframes where all other methods fail:

```bash
~/.claude/skills/axctl/scripts/axctl type "Google Chrome" --title "Field Name" "value"
```

Example for Amazon payment fields:
```bash
axctl type "Google Chrome" --title "Card number" "4111111111111111"
axctl type "Google Chrome" --title "Expiration date" "12/28"
```

Regular JS, Chrome `key` commands, `insert-text`, and `cliclick` all fail because the secure iframe blocks them. `axctl` works because it operates at the macOS accessibility layer, bypassing browser security entirely.

### cliclick for Native `<select>` Dropdowns

Secure iframes often render native `<select>` dropdowns that can't be changed via JS. Use `axctl` to find the dropdown's screen position, then `cliclick` to interact:

```bash
# Get the dropdown's position via accessibility
axctl get "Google Chrome" --title "Dropdown Label" AXPosition

# Click to open the native popup
cliclick c:<x>,<y>

# Click on the desired option at calculated coordinates
cliclick c:<x>,<option_y>
```

**Coordinate mapping from Chrome screenshot pixels to screen points:**
```
x_screen = x_screenshot * 1.2
y_screen = y_screenshot * 1.2 + 139
```
The `+ 139` offset accounts for Chrome's toolbar height (title bar + tab bar + address bar). Adjust if your toolbar configuration differs.

### Tab + Space for Iframe Buttons

When buttons inside secure iframes can't be clicked by any method (JS, CDP mouse events, `axctl` AXPress, `cliclick`), use keyboard navigation via CDP:

```bash
# Tab repeatedly to move focus into the iframe and onto the target button
chrome key <tab_id> "Tab"
chrome key <tab_id> "Tab"
chrome key <tab_id> "Tab"
# ... keep tabbing until the button is focused

# Activate the focused button
chrome key <tab_id> "Space"
```

**Why this works:** CDP `Input.dispatchKeyEvent` for Tab and Space crosses iframe boundaries, unlike mouse events which get blocked by cross-origin restrictions. Take screenshots between tabs to verify focus position.

### chrome click-at Uses CSS Viewport Coordinates

The `chrome click-at` command dispatches CDP `Input.dispatchMouseEvent`, which takes **CSS pixel viewport coordinates** (not screenshot pixels):

```bash
# Get actual viewport dimensions
chrome js <tab_id> "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"

# click-at uses these CSS viewport coordinates
chrome click-at <tab_id> <css_x> <css_y>
```

**Important:** Screenshot resolution often differs from viewport dimensions (e.g., a 1600px-wide screenshot may represent a 1920px-wide viewport on Retina displays). Always check `window.innerWidth`/`innerHeight` to understand the coordinate space.

### iframe-click Amazon Payment Iframe Fallback

The `iframe-click` command has been updated to detect Amazon payment iframes (`apx-secure-iframe`) and fall back to the first child frame if no known iframe pattern matches. This means `iframe-click` may work for some elements in payment iframes, but for text input and dropdowns, use `axctl` and `cliclick` as described above.
