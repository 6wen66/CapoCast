const DEFAULT_HOST = "10.0.0.162";

async function getHost() {
  const { host } = await chrome.storage.sync.get("host");
  return (host || DEFAULT_HOST).trim();
}

function parseYouTubeUrl(raw) {
  let u;
  try { u = new URL(raw); } catch { return {}; }
  const out = {};
  if (u.hostname === "youtu.be") out.videoId = u.pathname.slice(1);
  else if (/(^|\.)youtube\.com$/.test(u.hostname)) {
    if (u.pathname === "/watch") out.videoId = u.searchParams.get("v");
    else if (u.pathname.startsWith("/shorts/") || u.pathname.startsWith("/live/"))
      out.videoId = u.pathname.split("/")[2];
    else if (u.pathname === "/playlist") out.playlistId = u.searchParams.get("list");
    if (u.searchParams.get("list") && !out.playlistId && out.videoId)
      out.playlistId = u.searchParams.get("list");
  }
  return out;
}

function flash(tabId, text, color) {
  chrome.action.setBadgeBackgroundColor({ color, tabId });
  chrome.action.setBadgeText({ text, tabId });
  setTimeout(() => chrome.action.setBadgeText({ text: "", tabId }), 2500);
}

async function cast(tab, linkUrl) {
  const { videoId, playlistId } = parseYouTubeUrl(linkUrl || tab.url);
  if (!videoId && !playlistId) { flash(tab.id, "?", "#888"); return false; }

  const body = {};
  if (videoId) body.videoId = videoId;
  if (playlistId) body.playlistId = playlistId;

  // Resume where the local player is, unless we were given a link instead of the page itself.
  let player;
  if (!linkUrl && videoId) {
    try {
      [{ result: player }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const v = document.querySelector("video");
          if (!v) return null;
          const wasPlaying = !v.paused;
          v.pause();
          return { t: Math.floor(v.currentTime), wasPlaying };
        },
      });
    } catch { /* restricted page; cast from the start */ }
    if (player && player.t > 3) body.timestamp = player.t;
  }

  try {
    const host = await getHost();
    const res = await fetch(`http://${host}:8888/api/queue/play`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Playlet": "1" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    flash(tab.id, "OK", "#2e7d32");
    return true;
  } catch (e) {
    console.error("Playlet Cast failed:", e);
    chrome.action.setTitle({ tabId: tab.id, title: `Cast to Playlet - last error: ${e.message}` });
    flash(tab.id, "ERR", "#c62828");
    return false;
  }
}

chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "cast-now") return;
  tab ||= (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
  if (tab) cast(tab);
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "cast" && msg.tab) cast(msg.tab);
});

// ---- Roku Listen app bridge (YouTube tab) ---------------------------------------------------------
// feed.js (content script) sends player events (load / play / pause / seek) that we relay to the desktop
// app, which mirrors them onto Playlet; polls hand back the app's settings and queued commands.
// The app only accepts requests with this header.
const APP = "http://127.0.0.1:8765";
const APP_HEADERS = { "Content-Type": "application/json", "X-RokuListen": "1" };

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "event") {
    fetch(`${APP}/event`, { method: "POST", headers: APP_HEADERS, body: JSON.stringify(msg.event) })
      .then(() => sendResponse(true), () => sendResponse(false));
    return true;
  }
  if (msg.type === "poll") {
    fetch(`${APP}/command?${new URLSearchParams(msg.state || {})}`, { headers: APP_HEADERS })
      .then((r) => r.json())
      .then(sendResponse, () => sendResponse({}));
    return true;
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "cast-link",
    title: "Cast link to Playlet",
    contexts: ["link"],
    targetUrlPatterns: ["*://*.youtube.com/*", "*://youtu.be/*"],
  });
});

// Chrome doesn't put content scripts into pages that were already open when the extension (re)loads,
// so inject feed.js into open YouTube tabs ourselves; feed.js guards against running twice.
async function injectIntoOpenTabs() {
  const tabs = await chrome.tabs.query({ url: "*://www.youtube.com/*" });
  for (const t of tabs) {
    chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["feed.js"] }).catch(() => {});
  }
}
chrome.runtime.onInstalled.addListener(injectIntoOpenTabs);
chrome.runtime.onStartup.addListener(injectIntoOpenTabs);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "cast-link") cast(tab, info.linkUrl);
});
