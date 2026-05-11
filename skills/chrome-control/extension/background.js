// Chrome Control - Background Service Worker
// Native messaging bridge for local automation
// Full feature parity with Claude Chrome MCP

const NATIVE_HOST_NAME = 'com.dispatch.chrome_control';
let port = null;
let connecting = false;
let reconnectTimeout = null;

// Expected cadence of the native host's unsolicited keepalive ping (seconds).
// Mirrors PING_INTERVAL_S in scripts/native_host.py — kept here only as
// documentation of the contract; the worker reacts to whatever it receives.
const HOST_PING_INTERVAL_S = 20;

// Track managed tabs and console/network data
const managedTabs = new Map(); // tabId -> { url, status, createdAt }
const consoleMessages = new Map(); // tabId -> messages[]
const networkRequests = new Map(); // tabId -> requests[]

// TTL settings for stale tab cleanup (24 hours)
const TAB_TTL_MS = 24 * 60 * 60 * 1000;

// NOTE: Throughout this file, "grantUniveralAccess" (missing 's' in "Universal") is the
// correct spelling per the Chrome DevTools Protocol spec for Page.createIsolatedWorld.
// It is a known typo in the CDP API itself — do not "fix" it.

// === DEBUGGER PERSISTENCE OPTIMIZATION ===
// Keep debugger attached per-tab instead of attach/detach on every action
const attachedDebuggers = new Map(); // tabId -> { attachedAt, idleTimer, inUse }
const DEBUGGER_IDLE_TIMEOUT_MS = 30000; // 30 seconds idle timeout (increased from 10s)

// Ensure debugger is attached to tab (idempotent - no-op if already attached)
async function ensureDebuggerAttached(tabId) {
  const existing = attachedDebuggers.get(tabId);
  if (existing) {
    // Already attached - mark as in use (timer reset will happen in resetDebuggerIdleTimer)
    existing.inUse = true;
    return;
  }

  // Attach debugger with timeout to prevent hangs
  const attachWithTimeout = async () => {
    const attachPromise = chrome.debugger.attach({ tabId }, '1.3');
    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('debugger attach timeout (5s)')), 5000)
    );
    return Promise.race([attachPromise, timeoutPromise]);
  };

  try {
    await attachWithTimeout();
  } catch (e) {
    // If already attached (from previous session), that's fine - just track it
    if (e.message && e.message.includes('already attached')) {
      // Recovered from previous session
    } else {
      throw e;
    }
  }

  // Don't start idle timer yet - wait until operation completes
  attachedDebuggers.set(tabId, { attachedAt: Date.now(), idleTimer: null, inUse: true });
}

// Call this AFTER an operation completes to start/reset idle timer
function resetDebuggerIdleTimer(tabId) {
  const info = attachedDebuggers.get(tabId);
  if (!info) return;

  info.inUse = false;
  clearTimeout(info.idleTimer);
  info.idleTimer = setTimeout(() => detachDebuggerIdle(tabId), DEBUGGER_IDLE_TIMEOUT_MS);
}

// Detach debugger due to idle timeout
async function detachDebuggerIdle(tabId) {
  const info = attachedDebuggers.get(tabId);
  if (!info) return;

  attachedDebuggers.delete(tabId);
  try {
    await chrome.debugger.detach({ tabId });
    console.log(`[ChromeControl] Debugger detached from tab ${tabId} (idle timeout)`);
  } catch (e) {
    // Tab may have closed, ignore
  }
}

// Clean up debugger state when tab closes
chrome.tabs.onRemoved.addListener((tabId) => {
  const info = attachedDebuggers.get(tabId);
  if (info) {
    clearTimeout(info.idleTimer);
    attachedDebuggers.delete(tabId);
    console.log(`[ChromeControl] Debugger state cleaned up for closed tab ${tabId}`);
  }
});

// Handle debugger detach events (user clicked "cancel" on debugger bar, etc.)
chrome.debugger.onDetach.addListener((source) => {
  const tabId = source.tabId;
  const info = attachedDebuggers.get(tabId);
  if (info) {
    clearTimeout(info.idleTimer);
    attachedDebuggers.delete(tabId);
    console.log(`[ChromeControl] Debugger externally detached from tab ${tabId}`);
  }
});

// Connect to native messaging host
function connectNativeHost() {
  // Singleton guard: avoid duplicate spawns when both port.onDisconnect and
  // the keepalive alarm race to reconnect after the native host dies.
  if (port || connecting) return;
  connecting = true;

  console.log('[ChromeControl] Connecting to native host:', NATIVE_HOST_NAME);

  try {
    port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
    // Clear the in-flight flag shortly after; if the port is still alive
    // by then the connection succeeded, otherwise onDisconnect will reset it.
    setTimeout(() => { if (port) connecting = false; }, 250);

    port.onMessage.addListener(async (message) => {
      console.log('[ChromeControl] Received:', message);

      // Proactive keepalive from the native host (sent every ~HOST_PING_INTERVAL_S,
      // see native_host.py PING_INTERVAL_S). Just receiving this message already
      // reset the MV3 service worker's idle timer — the whole point. Reply with
      // a pong so the host can also detect a dead/wedged worker (no reply ⇒ gone).
      if (message.type === 'ping') {
        try {
          port.postMessage({ type: 'pong' });
        } catch (e) {
          console.log('[ChromeControl] pong failed:', e);
        }
        return;
      }

      if (message.type === 'ready') {
        console.log('[ChromeControl] Native host ready');

        // Get or create a unique profile ID (stored per-profile in chrome.storage.local)
        const getOrCreateProfileId = async () => {
          const result = await chrome.storage.local.get(['profileId']);
          if (result.profileId) {
            return result.profileId;
          }
          // Generate new UUID for this profile
          const newId = crypto.randomUUID();
          await chrome.storage.local.set({ profileId: newId });
          return newId;
        };

        // Get profile name from identity API, with fallback
        const getProfileName = () => new Promise((resolve) => {
          try {
            chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' }, (userInfo) => {
              if (chrome.runtime.lastError || !userInfo?.email) {
                resolve(null);
              } else {
                resolve(userInfo.email.split('@')[0]);
              }
            });
          } catch (e) {
            resolve(null);
          }
        });

        // Send ready with unique profile ID
        Promise.all([getOrCreateProfileId(), getProfileName()]).then(([profileId, profileName]) => {
          console.log('[ChromeControl] Registering with profileId:', profileId, 'name:', profileName);
          port.postMessage({
            type: 'extension_ready',
            extensionId: profileId,  // Use unique per-profile ID instead of chrome.runtime.id
            profileName: profileName,
            tabs: Array.from(managedTabs.entries()).map(([id, info]) => ({ id, ...info }))
          });
        });
        return;
      }

      if (message.type === 'reload') {
        console.log('[ChromeControl] Reload requested by native host - reloading extension');
        chrome.runtime.reload();
        return;
      }

      if (message.command) {
        try {
          const result = await handleCommand(message);
          if (message.id) {
            port.postMessage({ type: 'response', id: message.id, result });
          }
        } catch (error) {
          console.error('[ChromeControl] Error:', error);
          if (message.id) {
            port.postMessage({ type: 'response', id: message.id, error: error.message });
          }
        }
      }
    });

    port.onDisconnect.addListener(() => {
      console.log('[ChromeControl] Disconnected:', chrome.runtime.lastError?.message);
      port = null;
      connecting = false;
      scheduleReconnect();
    });

  } catch (error) {
    console.error('[ChromeControl] Connection failed:', error);
    port = null;
    connecting = false;
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (!reconnectTimeout) {
    reconnectTimeout = setTimeout(() => {
      reconnectTimeout = null;
      connectNativeHost();
    }, 5000);
  }
}

// Command handler - Full MCP parity
async function handleCommand(message) {
  const { command, params } = message;

  switch (command) {
    // Basic
    case 'ping':
      return { pong: true, timestamp: Date.now() };

    case 'profile_info':
      return await (async () => {
        const stored = await chrome.storage.local.get(['profileId']);
        return new Promise((resolve) => {
          chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' }, (userInfo) => {
            resolve({
              email: userInfo?.email || null,
              googleId: userInfo?.id || null,
              profileId: stored.profileId || null,
              extensionId: chrome.runtime.id,
              error: chrome.runtime.lastError?.message || null
            });
          });
        });
      })();

    // Tab management
    case 'tabs_context':
      return await getTabsContext();

    case 'tabs_create':
      return await createTab(params?.url);

    case 'open_tab':
      return await openTab(params.url);

    case 'close_tab':
      return await closeTab(params.tabId);

    case 'list_tabs':
      const tabs = await chrome.tabs.query({});
      return { tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title })) };

    // Navigation
    case 'navigate':
      return await navigateTab(params.tabId, params.url);

    // Page reading
    case 'read_page':
      return await readPage(params.tabId, params.filter, params.depth, params.ref_id);

    case 'find':
      return await findElements(params.tabId, params.query);

    case 'get_page_text':
      // Use isolated world to bypass CSP restrictions
      return await isolatedGetText(params.tabId);

    case 'get_page_html':
      // Use isolated world to bypass CSP restrictions
      return await isolatedGetHtml(params.tabId);

    // Interaction
    case 'click':
      return await executeInTab(params.tabId, 'click', params);

    case 'click_ref':
      return await clickByRef(params.tabId, params.ref);

    case 'click_at':
      return await clickAtCoordinates(params.tabId, params.x, params.y);

    case 'double_click':
      return await doubleClickAt(params.tabId, params.x, params.y);

    case 'type':
      return await executeInTab(params.tabId, 'type', params);

    case 'key':
      return await sendKey(params.tabId, params.key, params.modifiers);

    case 'insert_text':
      return await insertText(params.tabId, params.text);

    case 'iframe_click':
      return await iframeClick(params.tabId, params.selector);

    case 'iframe_type':
      return await iframeType(params.tabId, params.text);

    case 'iframe_debug':
      return await iframeDebug(params.tabId);

    case 'debugger_eval':
      return await debuggerEval(params.tabId, params.code || params.expression);

    case 'iframe_target_eval':
      return await iframeTargetEval(params.tabId, params.urlPattern, params.code);

    case 'iframe_target_type':
      return await iframeTargetType(params.tabId, params.urlPattern, params.text);

    case 'form_input':
      return await setFormValue(params.tabId, params.ref, params.value);

    case 'scroll':
      return await scroll(params.tabId, params.direction, params.amount, params.x, params.y);

    case 'scroll_to':
      return await scrollToElement(params.tabId, params.ref);

    case 'hover':
      return await hoverAt(params.tabId, params.x, params.y);

    // Screenshots
    case 'screenshot':
      return await takeScreenshot(params.tabId, message.id);

    case 'zoom':
      return await zoomScreenshot(params.tabId, params.region, message.id);

    // JavaScript execution
    case 'eval':
    case 'javascript':
      return await executeScript(params.tabId, params.code || params.text);

    case 'eval_all_frames':
      return await executeScriptAllFrames(params.tabId, params.code || params.text);

    case 'get_cookies':
      return await getCookies(params.domain || params.url);

    // Window management
    case 'resize_window':
      return await resizeWindow(params.tabId, params.width, params.height);

    case 'focus_tab':
      return await focusTab(params.tabId);

    // Console and Network
    case 'read_console':
      return await readConsole(params.tabId, params.pattern, params.limit, params.clear);

    case 'read_network':
      return await readNetwork(params.tabId, params.urlPattern, params.limit, params.clear);

    // Advanced - Debugger based
    case 'get_accessibility_tree':
      return await getAccessibilityTree(params.tabId);

    case 'click_by_name':
      return await clickByAccessibleName(params.tabId, params.name, params.role, params.index || 0);

    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

// Tab management
async function getTabsContext() {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  return {
    tabs: tabs.map(t => ({
      tabId: t.id,
      title: t.title,
      url: t.url,
      active: t.active
    }))
  };
}

async function createTab(url) {
  const tab = await chrome.tabs.create({ url: url || 'about:blank', active: true });
  return { tabId: tab.id, url: tab.url };
}

async function openTab(url) {
  const tab = await chrome.tabs.create({ url, active: true });
  const createdAt = Date.now();
  managedTabs.set(tab.id, { url, status: 'loading', createdAt });

  return new Promise((resolve) => {
    const listener = (tabId, changeInfo) => {
      if (tabId === tab.id && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        managedTabs.set(tab.id, { url, status: 'ready', createdAt });
        resolve({ tabId: tab.id, url, status: 'ready' });
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve({ tabId: tab.id, url, status: 'timeout' });
    }, 30000);
  });
}

async function closeTab(tabId) {
  await chrome.tabs.remove(tabId);
  managedTabs.delete(tabId);
  consoleMessages.delete(tabId);
  networkRequests.delete(tabId);
  return { closed: true };
}

async function navigateTab(tabId, url) {
  if (url === 'back') {
    await chrome.tabs.goBack(tabId);
  } else if (url === 'forward') {
    await chrome.tabs.goForward(tabId);
  } else {
    await chrome.tabs.update(tabId, { url });
  }
  return { navigated: true, url };
}

async function focusTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  await chrome.windows.update(tab.windowId, { focused: true });
  await chrome.tabs.update(tabId, { active: true });
  return { focused: true };
}

// Page reading
async function readPage(tabId, filter = 'all', depth = 15, refId = null) {
  const result = await executeInTab(tabId, 'get_elements', { filter, depth, refId });
  return result;
}

async function findElements(tabId, query) {
  const result = await executeInTab(tabId, 'find', { query });
  return result;
}

// Execute in content script
async function executeInTab(tabId, action, params) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { action, params });
    if (response.success) {
      return response.result;
    }
    // App-level error (element not found, etc.) - no re-injection needed
    throw new Error(response.error);
  } catch (error) {
    // Only re-inject for communication failures (content script not loaded)
    if (!error.message?.includes('Could not establish connection') &&
        !error.message?.includes('Receiving end does not exist')) {
      throw error;
    }
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js']
    });
    await new Promise(r => setTimeout(r, 100));
    const response = await chrome.tabs.sendMessage(tabId, { action, params });
    if (response.success) {
      return response.result;
    }
    throw new Error(response.error);
  }
}

// Execute arbitrary script
async function executeScript(tabId, code) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (code) => {
      try {
        return eval(code);
      } catch (e) {
        return { error: e.message };
      }
    },
    args: [code],
    world: 'MAIN'
  });
  return results?.[0]?.result;
}

// Execute script in ALL frames (including cross-origin iframes)
async function executeScriptAllFrames(tabId, code) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: (code) => {
      try {
        return eval(code);
      } catch (e) {
        return { error: e.message };
      }
    },
    args: [code],
    world: 'MAIN'
  });
  return results?.map(r => r.result).filter(r => r !== undefined && r !== null);
}

// Get all cookies for a domain (including HttpOnly)
async function getCookies(domain) {
  const cookies = await chrome.cookies.getAll({ domain: domain });
  return cookies.map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    httpOnly: c.httpOnly,
    secure: c.secure,
    sameSite: c.sameSite,
    expirationDate: c.expirationDate
  }));
}

// Click by element ref
async function clickByRef(tabId, ref) {
  return await executeInTab(tabId, 'click', { selector: ref });
}

// Click at coordinates using debugger (persistent attach)
async function clickAtCoordinates(tabId, x, y) {
  try {
    // CRITICAL: Input.dispatchMouseEvent requires the tab to be focused
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mousePressed', x, y, button: 'left', clickCount: 1
    });
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseReleased', x, y, button: 'left', clickCount: 1
    });

    resetDebuggerIdleTimer(tabId);
    return { clicked: true, x, y };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { clicked: false, error: error.message };
  }
}

async function doubleClickAt(tabId, x, y) {
  try {
    // CRITICAL: Input.dispatchMouseEvent requires the tab to be focused
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mousePressed', x, y, button: 'left', clickCount: 2
    });
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseReleased', x, y, button: 'left', clickCount: 2
    });

    resetDebuggerIdleTimer(tabId);
    return { clicked: true, x, y, double: true };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { clicked: false, error: error.message };
  }
}

async function hoverAt(tabId, x, y) {
  try {
    // CRITICAL: Input.dispatchMouseEvent requires the tab to be focused
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved', x, y
    });

    resetDebuggerIdleTimer(tabId);
    return { hovered: true, x, y };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { hovered: false, error: error.message };
  }
}

// Keyboard (persistent debugger)
async function sendKey(tabId, key, modifiers) {
  try {
    // CRITICAL: Input.dispatchKeyEvent requires the tab to be focused
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    const keyEvent = {
      type: 'keyDown',
      key: key,
      code: key,
      modifiers: 0
    };

    if (modifiers) {
      if (modifiers.includes('ctrl')) keyEvent.modifiers |= 2;
      if (modifiers.includes('alt')) keyEvent.modifiers |= 1;
      if (modifiers.includes('shift')) keyEvent.modifiers |= 8;
      if (modifiers.includes('meta') || modifiers.includes('cmd')) keyEvent.modifiers |= 4;
    }

    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', keyEvent);
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', { ...keyEvent, type: 'keyUp' });

    resetDebuggerIdleTimer(tabId);
    return { sent: true, key };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { sent: false, error: error.message };
  }
}

// Well-known cross-origin iframe domains for authentication and payment flows
const KNOWN_IFRAME_DOMAINS = [
  'idmsa.apple.com', 'signin.apple.com',
  'cybersource.com', 'flex.cybersource',
  'payments.amazon', 'apx-security'
];

// Find target iframe frame ID from a frame tree, searching for known cross-origin patterns.
// Returns the frame ID of the first matching frame, or null if none found.
function findTargetFrame(frameTree, knownDomains = KNOWN_IFRAME_DOMAINS) {
  let targetFrameId = null;
  const search = (frame) => {
    const url = frame.frame?.url || '';
    if (knownDomains.some(d => url.includes(d))) {
      targetFrameId = frame.frame?.id;
      return true;
    }
    if (frame.childFrames) {
      for (const child of frame.childFrames) {
        if (search(child)) return true;
      }
    }
    return false;
  };
  search(frameTree);
  return targetFrameId;
}

// Insert text via debugger using Page.getFrameTree to find iframes
async function insertText(tabId, text) {
  try {
    await ensureDebuggerAttached(tabId);

    // Enable Page domain to access frame tree
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    // Get frame tree
    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');
    console.log('[ChromeControl] Frame tree:', JSON.stringify(frameTree, null, 2));

    let targetFrameId = findTargetFrame(frameTree.frameTree);
    if (targetFrameId) {
      console.log('[ChromeControl] Found target auth/payment frame:', targetFrameId);
    }

    // For typing, we need to use a different approach:
    // Create an isolated world in the iframe and execute JS there
    if (targetFrameId) {
      // Create isolated world in the iframe
      const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
        frameId: targetFrameId,
        worldName: 'chromeControlWorld',
        grantUniveralAccess: true
      });
      const executionContextId = isolatedWorld.executionContextId;
      console.log('[ChromeControl] Created isolated world:', executionContextId);

      // Execute JS to type into the focused input
      // Handle both single inputs and multi-input 2FA code fields
      const script = `
        // Check for multi-input 2FA code fields first
        const allInputs = document.querySelectorAll('input');
        const codeInputs = document.querySelectorAll('input.form-security-code-input, input[type="tel"], input.code-input');
        const textToType = ${JSON.stringify(text)};

        // Debug info
        const debugInfo = {
          totalInputs: allInputs.length,
          codeInputs: codeInputs.length,
          inputClasses: Array.from(allInputs).map(i => i.className).join(', ')
        };

        if (codeInputs.length >= 6) {
          // Multi-input 2FA field - fill each input with one character
          const digits = textToType.split('');
          let filled = 0;
          Array.from(codeInputs).slice(0, digits.length).forEach((input, i) => {
            input.focus();
            // Use proper event simulation for React components
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(input, digits[i]);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            // Also try KeyboardEvent
            input.dispatchEvent(new KeyboardEvent('keydown', { key: digits[i], bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keypress', { key: digits[i], bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { key: digits[i], bubbles: true }));
            filled++;
          });
          JSON.stringify({ result: 'filled ' + filled + ' code inputs', ...debugInfo });
        } else if (codeInputs.length === 1) {
          // Single code input field (accepts all digits)
          const input = codeInputs[0];
          input.focus();
          input.value = textToType;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          JSON.stringify({ result: 'typed into single code input', ...debugInfo });
        } else {
          // Generic input field
          const input = document.activeElement || document.querySelector('input[type="text"], input[type="email"], input:not([type])');
          if (input && (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA')) {
            input.focus();
            input.value = textToType;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            JSON.stringify({ result: 'typed into generic input', className: input.className, ...debugInfo });
          } else {
            JSON.stringify({ result: 'no input found', activeElement: document.activeElement?.tagName, ...debugInfo });
          }
        }
      `;

      const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
        expression: script,
        contextId: executionContextId,
        returnByValue: true
      });
      console.log('[ChromeControl] Eval result:', result);

      resetDebuggerIdleTimer(tabId);
      return { inserted: true, text, frameId: targetFrameId, evalResult: result?.result?.value };
    } else {
      // No iframe found, type directly using Input.dispatchKeyEvent on main page
      for (const char of text) {
        await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
          type: 'rawKeyDown',
          key: char
        });
        await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
          type: 'keyUp',
          key: char
        });
        await new Promise(r => setTimeout(r, 10));
      }
      resetDebuggerIdleTimer(tabId);
      return { inserted: true, text, frameId: null, note: 'no iframe found, used main page' };
    }
  } catch (error) {
    console.error('[ChromeControl] insertText error:', error);
    resetDebuggerIdleTimer(tabId);
    return { inserted: false, error: error.message };
  }
}

// Debug iframe contents
async function iframeDebug(tabId) {
  try {
    await ensureDebuggerAttached(tabId);
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');

    let targetFrameId = findTargetFrame(frameTree.frameTree);

    if (targetFrameId) {
      const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
        frameId: targetFrameId,
        worldName: 'chromeControlDebug',
        grantUniveralAccess: true
      });

      const script = `
        const allInputs = document.querySelectorAll('input');
        const result = {
          totalInputs: allInputs.length,
          inputs: Array.from(allInputs).map(i => ({
            type: i.type,
            name: i.name,
            id: i.id,
            className: i.className,
            maxLength: i.maxLength,
            value: i.value
          }))
        };
        JSON.stringify(result, null, 2);
      `;

      const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
        expression: script,
        contextId: isolatedWorld.executionContextId,
        returnByValue: true
      });

      resetDebuggerIdleTimer(tabId);
      return { debug: JSON.parse(result?.result?.value || '{}') };
    } else {
      resetDebuggerIdleTimer(tabId);
      return { error: 'iframe not found' };
    }
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { error: error.message };
  }
}

// Execute arbitrary JS via debugger API (bypasses CSP)
async function debuggerEval(tabId, code) {
  try {
    await ensureDebuggerAttached(tabId);
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');
    const mainFrameId = frameTree.frameTree.frame.id;

    // Create isolated world in main frame with universal access
    const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
      frameId: mainFrameId,
      worldName: 'chromeControlEval',
      grantUniveralAccess: true
    });

    // Wrap code to return JSON result
    const wrappedCode = `
      try {
        const __result = (function() { ${code} })();
        JSON.stringify({ success: true, result: __result });
      } catch (e) {
        JSON.stringify({ success: false, error: e.message, stack: e.stack });
      }
    `;

    const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
      expression: wrappedCode,
      contextId: isolatedWorld.executionContextId,
      returnByValue: true
    });

    try {
      return JSON.parse(result?.result?.value || '{}');
    } catch {
      return { raw: result?.result?.value };
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
}

// Type into iframe using debugger key events
async function iframeType(tabId, text) {
  try {
    await ensureDebuggerAttached(tabId);

    // Type each character using Input.dispatchKeyEvent
    for (const char of text) {
      // rawKeyDown (no text insertion - avoids double-typing with char event)
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'rawKeyDown',
        key: char,
        windowsVirtualKeyCode: char.charCodeAt(0),
        nativeVirtualKeyCode: char.charCodeAt(0)
      });
      // Char event
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'char',
        key: char,
        text: char,
        unmodifiedText: char
      });
      // Key up
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'keyUp',
        key: char,
        windowsVirtualKeyCode: char.charCodeAt(0),
        nativeVirtualKeyCode: char.charCodeAt(0)
      });
      await new Promise(r => setTimeout(r, 50));
    }

    return { typed: true, text };
  } catch (error) {
    return { typed: false, error: error.message };
  }
}

// Click element in iframe via isolated world
async function iframeClick(tabId, selector) {
  try {
    await ensureDebuggerAttached(tabId);
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');

    let targetFrameId = findTargetFrame(frameTree.frameTree);
    // If no known iframe found, use the first child iframe
    if (!targetFrameId && frameTree.frameTree.childFrames?.length > 0) {
      targetFrameId = frameTree.frameTree.childFrames[0].frame?.id;
    }

    // Use iframe if found, otherwise fall back to main frame (for CSP-protected pages like Google Cloud Console)
    const useFrameId = targetFrameId || frameTree.frameTree.frame.id;

    const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
      frameId: useFrameId,
      worldName: 'chromeControlClickWorld',
      grantUniveralAccess: true
    });

    // More robust click: dispatch full mouse event sequence + focus + click
    // Also supports text:XXX selector to click by text content
    // Searches both regular DOM AND shadow DOMs for cookie consent modals
    const script = `
      const selector = ${JSON.stringify(selector)};
      let el = null;

      // Helper function to search through shadow DOMs recursively
      function querySelectorAllDeep(root, selectors) {
        const elements = [...root.querySelectorAll(selectors)];
        const shadowRoots = [...root.querySelectorAll('*')].filter(e => e.shadowRoot);
        shadowRoots.forEach(e => {
          elements.push(...querySelectorAllDeep(e.shadowRoot, selectors));
        });
        return elements;
      }

      // Check if it's a text selector
      if (selector.startsWith('text:')) {
        const searchText = selector.substring(5).toLowerCase();
        // Search both regular DOM and shadow DOMs
        const allClickable = querySelectorAllDeep(document, 'button, [role="button"], input[type="submit"], a, [onclick], div[class*="button"], span[class*="button"]');

        // First pass: look for exact or very close matches (button with just this text)
        for (const candidate of allClickable) {
          const text = (candidate.textContent || candidate.value || '').toLowerCase().trim();
          // Exact match or starts with the search text (for cases like "Accept all" vs "Accept allAccept...")
          if (text === searchText || text.startsWith(searchText + ' ') || (text.includes(searchText) && text.length < searchText.length * 2)) {
            el = candidate;
            break;
          }
        }
        // Second pass: if no good match, look for any element containing the text
        if (!el) {
          for (const candidate of allClickable) {
            const text = (candidate.textContent || candidate.value || '').toLowerCase().trim();
            if (text.includes(searchText)) {
              el = candidate;
              break;
            }
          }
        }
      } else {
        // First try regular DOM
        el = document.querySelector(selector);
        // If not found, search shadow DOMs
        if (!el) {
          const deepElements = querySelectorAllDeep(document, selector);
          el = deepElements[0] || null;
        }
      }

      if (el) {
        // Focus first
        el.focus();

        // Get element center for coordinates
        const rect = el.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;

        // Create proper mouse event options
        const eventInit = {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: x,
          clientY: y,
          screenX: x,
          screenY: y,
          button: 0,
          buttons: 1
        };

        // Dispatch full mouse event sequence
        el.dispatchEvent(new MouseEvent('mouseenter', eventInit));
        el.dispatchEvent(new MouseEvent('mouseover', eventInit));
        el.dispatchEvent(new MouseEvent('mousemove', eventInit));
        el.dispatchEvent(new MouseEvent('mousedown', { ...eventInit, buttons: 1 }));
        el.dispatchEvent(new MouseEvent('mouseup', { ...eventInit, buttons: 0 }));
        el.dispatchEvent(new MouseEvent('click', { ...eventInit, buttons: 0 }));

        // Also try the native click as backup
        el.click();

        JSON.stringify({
          success: true,
          text: el.textContent.trim().substring(0, 50),
          tagName: el.tagName,
          type: el.type || null,
          id: el.id || null,
          className: el.className || null,
          usedMainFrame: !${targetFrameId ? 'true' : 'false'}
        });
      } else {
        // Try to find buttons with similar text - also search shadow DOMs
        const allButtons = querySelectorAllDeep(document, 'button, [role="button"], input[type="submit"], a');
        const buttonTexts = allButtons.map(b => b.textContent?.trim().substring(0, 30) || b.value || '').filter(t => t);
        JSON.stringify({
          success: false,
          error: 'element not found: ' + selector,
          availableButtons: buttonTexts.slice(0, 10)
        });
      }
    `;

    const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
      expression: script,
      contextId: isolatedWorld.executionContextId,
      returnByValue: true
    });

    let parsedResult;
    try {
      parsedResult = JSON.parse(result?.result?.value || '{}');
    } catch {
      parsedResult = { raw: result?.result?.value };
    }

    return { clicked: parsedResult.success !== false, result: parsedResult, usedMainFrame: !targetFrameId };
  } catch (error) {
    return { clicked: false, error: error.message };
  }
}

// Get page text via isolated world (bypasses CSP)
async function isolatedGetText(tabId) {
  try {
    await ensureDebuggerAttached(tabId);
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');
    const mainFrameId = frameTree.frameTree.frame.id;

    const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
      frameId: mainFrameId,
      worldName: 'chromeControlTextWorld',
      grantUniveralAccess: true
    });

    const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
      expression: 'document.body.innerText',
      contextId: isolatedWorld.executionContextId,
      returnByValue: true
    });

    resetDebuggerIdleTimer(tabId);
    return result?.result?.value || '';
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { error: error.message };
  }
}

// Get page HTML via isolated world (bypasses CSP)
async function isolatedGetHtml(tabId) {
  try {
    await ensureDebuggerAttached(tabId);
    await chrome.debugger.sendCommand({ tabId }, 'Page.enable');

    const frameTree = await chrome.debugger.sendCommand({ tabId }, 'Page.getFrameTree');
    const mainFrameId = frameTree.frameTree.frame.id;

    const isolatedWorld = await chrome.debugger.sendCommand({ tabId }, 'Page.createIsolatedWorld', {
      frameId: mainFrameId,
      worldName: 'chromeControlHtmlWorld',
      grantUniveralAccess: true
    });

    const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
      expression: 'document.documentElement.outerHTML',
      contextId: isolatedWorld.executionContextId,
      returnByValue: true
    });

    resetDebuggerIdleTimer(tabId);
    return result?.result?.value || '';
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { error: error.message };
  }
}

// Form input
async function setFormValue(tabId, ref, value) {
  return await executeInTab(tabId, 'set_value', { selector: ref, value });
}

// Scrolling (persistent debugger)
async function scroll(tabId, direction, amount = 3, x, y) {
  const scrollAmount = amount * 100;
  let deltaX = 0, deltaY = 0;

  switch (direction) {
    case 'up': deltaY = -scrollAmount; break;
    case 'down': deltaY = scrollAmount; break;
    case 'left': deltaX = -scrollAmount; break;
    case 'right': deltaX = scrollAmount; break;
  }

  try {
    // CRITICAL: Input.dispatchMouseEvent requires the tab to be focused
    // See: https://github.com/ChromeDevTools/devtools-protocol/issues/89
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x: x || 400,
      y: y || 400,
      deltaX,
      deltaY
    });

    resetDebuggerIdleTimer(tabId);
    return { scrolled: true, direction, amount };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { scrolled: false, error: error.message };
  }
}

async function scrollToElement(tabId, ref) {
  return await executeInTab(tabId, 'scroll_to', { selector: ref });
}

// Screenshot
async function takeScreenshot(tabId, requestId) {
  const tab = await chrome.tabs.get(tabId);
  await chrome.windows.update(tab.windowId, { focused: true });
  await chrome.tabs.update(tabId, { active: true });
  await new Promise(r => setTimeout(r, 100));

  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 80 });

  const base64Data = dataUrl.split(',')[1];

  // Use larger chunks (200KB) and add delays to prevent service worker suspension
  const CHUNK_SIZE = 200000;
  const totalChunks = Math.ceil(base64Data.length / CHUNK_SIZE);

  console.log(`[ChromeControl] Sending ${totalChunks} screenshot chunks`);

  for (let i = 0; i < totalChunks; i++) {
    // Check port is still valid
    if (!port) {
      throw new Error('Native host disconnected during screenshot');
    }

    const chunk = base64Data.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
    try {
      port.postMessage({
        type: 'screenshot_chunk',
        requestId,
        index: i,
        total: totalChunks,
        data: chunk,
        format: 'jpeg'
      });
      console.log(`[ChromeControl] Sent chunk ${i + 1}/${totalChunks}`);
    } catch (e) {
      console.error(`[ChromeControl] Failed to send chunk ${i + 1}:`, e);
      throw e;
    }

    // Small delay between chunks to prevent overwhelming native messaging
    if (i < totalChunks - 1) {
      await new Promise(r => setTimeout(r, 10));
    }
  }

  console.log('[ChromeControl] All chunks sent');
  return { screenshotChunked: true, chunks: totalChunks };
}

async function zoomScreenshot(tabId, region, requestId) {
  // For zoom, we capture full and crop on client side
  // Just return the region info with screenshot
  const result = await takeScreenshot(tabId, requestId);
  result.region = region;
  return result;
}

// Window resize
async function resizeWindow(tabId, width, height) {
  const tab = await chrome.tabs.get(tabId);
  await chrome.windows.update(tab.windowId, { width, height });
  return { resized: true, width, height };
}

// Console and Network monitoring
async function readConsole(tabId, pattern, limit = 100, clear = false) {
  const messages = consoleMessages.get(tabId) || [];
  let filtered = messages;

  if (pattern) {
    const regex = new RegExp(pattern, 'i');
    filtered = messages.filter(m => regex.test(m.text));
  }

  if (limit) {
    filtered = filtered.slice(-limit);
  }

  if (clear) {
    consoleMessages.set(tabId, []);
  }

  return { messages: filtered };
}

async function readNetwork(tabId, urlPattern, limit = 100, clear = false) {
  const requests = networkRequests.get(tabId) || [];
  let filtered = requests;

  if (urlPattern) {
    filtered = requests.filter(r => r.url.includes(urlPattern));
  }

  if (limit) {
    filtered = filtered.slice(-limit);
  }

  if (clear) {
    networkRequests.set(tabId, []);
  }

  return { requests: filtered };
}

// Evaluate JS in cross-origin iframe via Target.setAutoAttach + sessionId
// This is the proper way to interact with isolated iframe contexts (e.g. CyberSource payment)
async function iframeTargetEval(tabId, urlPattern, code) {
  try {
    await ensureDebuggerAttached(tabId);

    // Enable required domains
    await chrome.debugger.sendCommand({ tabId }, 'Target.setAutoAttach', {
      autoAttach: true,
      flatten: true,
      waitForDebuggerOnStart: false
    });
    await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable');

    // Wait a bit for iframes to attach
    await new Promise(r => setTimeout(r, 500));

    // Get all targets
    const { targetInfos } = await chrome.debugger.sendCommand({ tabId }, 'Target.getTargets');
    console.log('[ChromeControl] Available targets:', JSON.stringify(targetInfos, null, 2));

    // Find iframe matching the pattern
    const iframeTarget = targetInfos.find(t =>
      t.type === 'iframe' && t.url && t.url.includes(urlPattern)
    );

    if (!iframeTarget) {
      return {
        success: false,
        error: `No iframe found matching "${urlPattern}"`,
        availableTargets: targetInfos.filter(t => t.type === 'iframe').map(t => ({
          type: t.type,
          url: t.url
        }))
      };
    }

    console.log('[ChromeControl] Found iframe target:', iframeTarget);

    // Attach to the iframe target specifically
    const { sessionId } = await chrome.debugger.sendCommand({ tabId }, 'Target.attachToTarget', {
      targetId: iframeTarget.targetId,
      flatten: true
    });

    console.log('[ChromeControl] Attached to iframe, sessionId:', sessionId);

    // Enable Runtime in the iframe context
    await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable', {}, sessionId);

    // Execute the code in the iframe
    const result = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
      expression: code,
      returnByValue: true
    }, sessionId);

    console.log('[ChromeControl] Eval result:', result);

    return {
      success: true,
      result: result?.result?.value,
      targetUrl: iframeTarget.url
    };
  } catch (error) {
    console.error('[ChromeControl] iframeTargetEval error:', error);
    return { success: false, error: error.message };
  }
}

// Type into cross-origin iframe using Target.setAutoAttach + Input.dispatchKeyEvent with sessionId
async function iframeTargetType(tabId, urlPattern, text) {
  try {
    await ensureDebuggerAttached(tabId);

    // Enable auto-attach with flatten mode
    await chrome.debugger.sendCommand({ tabId }, 'Target.setAutoAttach', {
      autoAttach: true,
      flatten: true,
      waitForDebuggerOnStart: false
    });

    // Wait for iframes to attach
    await new Promise(r => setTimeout(r, 500));

    // Get all targets
    const { targetInfos } = await chrome.debugger.sendCommand({ tabId }, 'Target.getTargets');

    // Find iframe matching the pattern
    const iframeTarget = targetInfos.find(t =>
      t.type === 'iframe' && t.url && t.url.includes(urlPattern)
    );

    if (!iframeTarget) {
      return {
        success: false,
        error: `No iframe found matching "${urlPattern}"`,
        availableTargets: targetInfos.filter(t => t.type === 'iframe').map(t => t.url)
      };
    }

    // Attach to the iframe target
    const { sessionId } = await chrome.debugger.sendCommand({ tabId }, 'Target.attachToTarget', {
      targetId: iframeTarget.targetId,
      flatten: true
    });

    console.log('[ChromeControl] Attached to iframe for typing, sessionId:', sessionId);

    // Type each character using Input.dispatchKeyEvent with sessionId
    for (const char of text) {
      // keyDown
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'rawKeyDown',
        key: char,
        windowsVirtualKeyCode: char.charCodeAt(0),
        nativeVirtualKeyCode: char.charCodeAt(0)
      }, sessionId);

      // char event
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'char',
        key: char,
        text: char,
        unmodifiedText: char
      }, sessionId);

      // keyUp
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'keyUp',
        key: char,
        windowsVirtualKeyCode: char.charCodeAt(0),
        nativeVirtualKeyCode: char.charCodeAt(0)
      }, sessionId);

      await new Promise(r => setTimeout(r, 30));
    }

    return { success: true, typed: text, targetUrl: iframeTarget.url };
  } catch (error) {
    console.error('[ChromeControl] iframeTargetType error:', error);
    return { success: false, error: error.message };
  }
}

// Accessibility tree via debugger (persistent)
async function getAccessibilityTree(tabId) {
  try {
    await ensureDebuggerAttached(tabId);
    const tree = await chrome.debugger.sendCommand({ tabId }, 'Accessibility.getFullAXTree');
    return tree;
  } catch (error) {
    return { error: error.message };
  }
}

// Click element by accessible name using Chrome's accessibility tree
// This bypasses CSP restrictions since it uses the debugger protocol
async function clickByAccessibleName(tabId, name, role = null, index = 0) {
  try {
    // Focus the tab first
    const tab = await chrome.tabs.get(tabId);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tabId, { active: true });

    await ensureDebuggerAttached(tabId);

    // Enable Accessibility domain
    await chrome.debugger.sendCommand({ tabId }, 'Accessibility.enable');

    // Get the full accessibility tree
    const tree = await chrome.debugger.sendCommand({ tabId }, 'Accessibility.getFullAXTree');

    if (!tree || !tree.nodes) {
      resetDebuggerIdleTimer(tabId);
      return { clicked: false, error: 'No accessibility tree available' };
    }

    // Search for nodes matching the name (case-insensitive partial match)
    const searchName = name.toLowerCase();
    const matches = [];

    for (const node of tree.nodes) {
      // Check name property
      const nodeName = node.name?.value?.toLowerCase() || '';
      const nodeRole = node.role?.value || '';

      // Skip ignored/invisible nodes
      if (node.ignored) continue;

      // Match by name (partial, case-insensitive)
      const nameMatches = nodeName.includes(searchName) || searchName.includes(nodeName);

      // Match by role if specified
      const roleMatches = !role || nodeRole.toLowerCase() === role.toLowerCase();

      if (nameMatches && roleMatches && nodeName) {
        matches.push({
          nodeId: node.nodeId,
          backendNodeId: node.backendDOMNodeId,
          name: node.name?.value,
          role: nodeRole,
          description: node.description?.value
        });
      }
    }

    if (matches.length === 0) {
      resetDebuggerIdleTimer(tabId);
      // Return available buttons/links for debugging
      const clickables = tree.nodes
        .filter(n => !n.ignored && ['button', 'link', 'menuitem', 'checkbox', 'radio'].includes(n.role?.value?.toLowerCase()))
        .map(n => ({ name: n.name?.value, role: n.role?.value }))
        .filter(n => n.name)
        .slice(0, 15);
      return {
        clicked: false,
        error: `No element found with name containing "${name}"`,
        matchesFound: 0,
        availableElements: clickables
      };
    }

    // Get the match at the specified index
    const match = matches[index];
    if (!match) {
      resetDebuggerIdleTimer(tabId);
      return {
        clicked: false,
        error: `Index ${index} out of range. Found ${matches.length} matches.`,
        matchesFound: matches.length,
        matches: matches.slice(0, 5).map(m => ({ name: m.name, role: m.role }))
      };
    }

    // Use DOM.getBoxModel to get the element's position via backendNodeId
    const boxModel = await chrome.debugger.sendCommand({ tabId }, 'DOM.getBoxModel', {
      backendNodeId: match.backendNodeId
    });

    if (!boxModel || !boxModel.model) {
      // Fallback: try using nodeId from DOM tree
      await chrome.debugger.sendCommand({ tabId }, 'DOM.enable');
      const doc = await chrome.debugger.sendCommand({ tabId }, 'DOM.getDocument');

      resetDebuggerIdleTimer(tabId);
      return {
        clicked: false,
        error: 'Could not get element position',
        match: { name: match.name, role: match.role }
      };
    }

    // Get center of the content box
    const content = boxModel.model.content;
    // content is [x1,y1, x2,y2, x3,y3, x4,y4] - corners of the quad
    const x = Math.round((content[0] + content[2] + content[4] + content[6]) / 4);
    const y = Math.round((content[1] + content[3] + content[5] + content[7]) / 4);

    // Click at the center
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mousePressed', x, y, button: 'left', clickCount: 1
    });
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseReleased', x, y, button: 'left', clickCount: 1
    });

    resetDebuggerIdleTimer(tabId);
    return {
      clicked: true,
      name: match.name,
      role: match.role,
      x, y,
      matchesFound: matches.length
    };
  } catch (error) {
    resetDebuggerIdleTimer(tabId);
    return { clicked: false, error: error.message };
  }
}

// Initialize
connectNativeHost();

// Set up keepalive alarm to prevent service worker suspension.
// 0.5 min (30s) is Chrome's minimum periodic-alarm interval — anything smaller
// is silently clamped to 30s and spams a console warning on every worker load.
// This alarm is also our recovery backstop: if the worker is ever evicted, the
// alarm fires, the worker wakes, runs the heartbeat handler below, and
// reconnects the native host without any manual intervention.
function setupKeepaliveAlarm() {
  chrome.alarms.create('keepalive', { periodInMinutes: 0.5 }); // 30s — Chrome's minimum
}

// Set up hourly alarm for stale tab cleanup
function setupStaleTabCleanupAlarm() {
  chrome.alarms.create('stale_tab_cleanup', { periodInMinutes: 60 }); // every hour
}

// Clean up stale tabs opened by the extension
async function cleanupStaleTabs() {
  const now = Date.now();
  const closedTabs = [];

  for (const [tabId, info] of managedTabs.entries()) {
    // Skip if no createdAt (legacy tabs before this feature)
    if (!info.createdAt) continue;

    const age = now - info.createdAt;
    if (age > TAB_TTL_MS) {
      try {
        // Check if tab still exists and is not pinned
        const tab = await chrome.tabs.get(tabId);
        if (!tab.pinned) {
          await chrome.tabs.remove(tabId);
          managedTabs.delete(tabId);
          consoleMessages.delete(tabId);
          networkRequests.delete(tabId);
          closedTabs.push({ tabId, url: info.url, ageHours: Math.round(age / 3600000) });
          console.log(`[ChromeControl] Closed stale tab ${tabId} (${info.url}) - age: ${Math.round(age / 3600000)}h`);
        }
      } catch (e) {
        // Tab no longer exists, clean up tracking
        managedTabs.delete(tabId);
        consoleMessages.delete(tabId);
        networkRequests.delete(tabId);
      }
    }
  }

  if (closedTabs.length > 0) {
    console.log(`[ChromeControl] Stale tab cleanup: closed ${closedTabs.length} tabs`);
  }

  return closedTabs;
}

chrome.runtime.onStartup.addListener(() => {
  connectNativeHost();
  setupKeepaliveAlarm();
  setupStaleTabCleanupAlarm();
});

chrome.runtime.onInstalled.addListener(() => {
  connectNativeHost();
  setupKeepaliveAlarm();
  setupStaleTabCleanupAlarm();
});

// Handle alarms - keepalive and stale tab cleanup
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'keepalive') {
    if (port) {
      try {
        port.postMessage({ type: 'heartbeat' });
        console.log('[ChromeControl] Keepalive heartbeat sent');
      } catch (e) {
        console.log('[ChromeControl] Heartbeat failed, reconnecting');
        port = null;
        connecting = false;
        connectNativeHost();
      }
    } else {
      console.log('[ChromeControl] No port, reconnecting');
      connectNativeHost();
    }
  } else if (alarm.name === 'stale_tab_cleanup') {
    const closedTabs = await cleanupStaleTabs();
    // Send log to native host for file logging
    if (closedTabs.length > 0 && port) {
      try {
        port.postMessage({
          type: 'stale_tabs_closed',
          tabs: closedTabs,
          timestamp: new Date().toISOString()
        });
      } catch (e) {
        console.log('[ChromeControl] Failed to log stale tab cleanup:', e);
      }
    }
  }
});

// Initial alarm setup
setupKeepaliveAlarm();
setupStaleTabCleanupAlarm();

// Listen for console messages from content script
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type === 'console_message' && sender.tab) {
    const tabId = sender.tab.id;
    if (!consoleMessages.has(tabId)) {
      consoleMessages.set(tabId, []);
    }
    consoleMessages.get(tabId).push(message.data);
  }
});
