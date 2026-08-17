// recover.js — calls chrome.runtime.reload() from a page renderer.
//
// This context is NOT the service worker. It keeps working when the worker is
// evicted, crashed, or "deaf" (running but firing no listeners), which is the
// whole point: chrome.runtime.reload() from here restarts the extension the
// same way a human clicking ⟳ on chrome://extensions does.
//
// Lifecycle note (verified empirically, 2026-08): chrome.runtime.reload()
// tears down every extension renderer, including this page — so any code after
// it will usually never run. The tab is therefore cleaned up two ways:
//   1. best-effort self-close scheduled BEFORE the reload, in case the page
//      somehow survives;
//   2. chrome-heal closes any leftover recover.html tab once `chrome ping`
//      goes green (the authoritative cleanup).

const DELAY_BEFORE_RELOAD_MS = 400; // let the status text paint first
const SELF_CLOSE_MS = 2500; // only fires if the reload did not tear us down

const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
const detailEl = document.getElementById("detail");

function setStatus(text, kind) {
  statusEl.textContent = text;
  dot.className = "dot" + (kind ? " " + kind : "");
}

function detail(text) {
  detailEl.textContent = text;
}

function stamp() {
  return new Date().toLocaleTimeString();
}

async function selfClose() {
  // Extension pages are allowed to close their own tab.
  try {
    const tab = await chrome.tabs.getCurrent();
    if (tab && typeof tab.id === "number") {
      await chrome.tabs.remove(tab.id);
      return;
    }
  } catch (_) {
    /* chrome.tabs may be unavailable if the extension is mid-reload */
  }
  try {
    window.close();
  } catch (_) {
    /* nothing left to try; chrome-heal cleans the tab up */
  }
}

function main() {
  const id = (chrome.runtime && chrome.runtime.id) || "unknown";

  if (!chrome.runtime || typeof chrome.runtime.reload !== "function") {
    setStatus("chrome.runtime.reload() is unavailable", "err");
    detail(
      "This page is not running in an extension context, so it cannot reload " +
        "anything. Open it as chrome-extension://<extension-id>/recover.html."
    );
    return;
  }

  setStatus("Reloading extension " + id + "…");
  detail(
    "Triggered at " +
      stamp() +
      ".\nThis tab closes itself once the reload lands. If it is still here, " +
      "the reload already tore down this renderer and chrome-heal will close it."
  );

  // Scheduled BEFORE the reload: if reload() kills this renderer (the normal
  // case) this timer simply never fires.
  setTimeout(selfClose, SELF_CLOSE_MS);

  setTimeout(() => {
    try {
      chrome.runtime.reload();
      // Usually unreachable — the renderer is gone by now.
      setStatus("Reload requested", "ok");
    } catch (e) {
      setStatus("Reload failed: " + (e && e.message ? e.message : String(e)), "err");
      detail(
        "chrome.runtime.reload() threw. Chrome rate-limits repeated reloads " +
          "(roughly 1 per 10s per extension); wait ~10s and reload this page, " +
          "or fall back to chrome://extensions ⟳."
      );
    }
  }, DELAY_BEFORE_RELOAD_MS);
}

main();
