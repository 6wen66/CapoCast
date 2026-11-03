const $ = (id) => document.getElementById(id);
const DEFAULT_HOST = "10.0.0.162";
const POLL_MS = 1000;

let host = DEFAULT_HOST;
let nowPlaying = null;      // { videoid, title, thumbnail } from Playlet's queue
let state = "none";         // ECP player state: play | pause | buffering | none ...
let positionSec = 0;
let durationSec = 0;
let dragging = false;
let ecpAllowed = true;

const fmt = (s) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(r).padStart(2, "0")}`;
};
const ms = (txt) => (parseInt(txt, 10) || 0) / 1000;

const ecp = (path, opts) => fetch(`http://${host}:8060${path}`, opts);
const playlet = (path, body) =>
  fetch(`http://${host}:8888${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Playlet": "1" },
    body: JSON.stringify(body),
  });

async function refresh() {
  try {
    const q = await (await fetch(`http://${host}:8888/api/queue`)).json();
    nowPlaying = q.nowplaying || null;
  } catch {
    $("sub").textContent = "Can't reach Playlet - is it open on the Roku?";
    return render(false);
  }

  try {
    const res = await ecp("/query/media-player");
    if (res.status === 403) { ecpAllowed = false; }
    else {
      ecpAllowed = true;
      const xml = new DOMParser().parseFromString(await res.text(), "text/xml");
      state = xml.querySelector("player")?.getAttribute("state") || "none";
      positionSec = ms(xml.querySelector("position")?.textContent);
      durationSec = ms(xml.querySelector("duration")?.textContent);
    }
  } catch { ecpAllowed = false; }
  render(true);
}

function render(reachable) {
  const has = reachable && nowPlaying;
  $("title").textContent = has ? (nowPlaying.title || nowPlaying.videoid) : "Nothing playing";
  if (has && nowPlaying.thumbnail) $("thumb").src = nowPlaying.thumbnail;
  $("thumb").classList.toggle("clickable", !!has);
  $("thumb").title = has ? "Open in a new tab" : "";

  if (reachable) {
    $("sub").textContent = !ecpAllowed ? "Live controls locked" :
      state === "play" ? "Playing" : state === "pause" ? "Paused" :
      state === "buffering" ? "Buffering…" : "Idle";
  }

  const live = reachable && ecpAllowed && durationSec > 0;
  for (const id of ["bar", "back", "toggle", "fwd"]) $(id).disabled = !live;
  $("toggle").textContent = state === "pause" ? "▶" : "⏸";

  if (live && !dragging) {
    $("bar").max = durationSec;
    $("bar").value = positionSec;
    $("pos").textContent = fmt(positionSec);
    $("dur").textContent = fmt(durationSec);
  }

  const b = $("banner");
  b.hidden = ecpAllowed;
  if (!ecpAllowed) b.innerHTML =
    "Roku is blocking remote control. On the TV: <b>Settings → System → Advanced system settings → " +
    "Control by mobile apps → Network access → Permissive</b>.";
}

// Absolute seek: Playlet has no seek API, so re-send the video at a new timestamp.
async function seekTo(sec) {
  if (!nowPlaying) return;
  sec = Math.max(0, Math.min(sec, Math.max(0, durationSec - 2)));
  positionSec = sec;
  $("pos").textContent = fmt(sec);
  await playlet("/api/queue/play", { videoId: nowPlaying.videoid, timestamp: Math.floor(sec) });
  setTimeout(refresh, 1500);
}

$("bar").addEventListener("input", () => { dragging = true; $("pos").textContent = fmt($("bar").value); });
$("bar").addEventListener("change", async () => { await seekTo(Number($("bar").value)); dragging = false; });
$("back").onclick = () => seekTo(positionSec - 10);
$("fwd").onclick = () => seekTo(positionSec + 10);
$("toggle").onclick = async () => {
  state = state === "pause" ? "play" : "pause";   // optimistic; next poll corrects it
  render(true);
  await ecp("/keypress/Play", { method: "POST" });
};
// Pop the now-playing video out into a browser tab, resuming at the Roku's position.
$("thumb").onclick = () => {
  if (!nowPlaying?.videoid) return;
  const url = new URL("https://www.youtube.com/watch");
  url.searchParams.set("v", nowPlaying.videoid);
  if (positionSec > 3) url.searchParams.set("t", `${Math.floor(positionSec)}s`);
  chrome.tabs.create({ url: url.href });
};
$("cast").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.runtime.sendMessage({ type: "cast", tab });
  $("sub").textContent = "Casting…";
  setTimeout(refresh, 2000);
};

(async () => {
  ({ host = DEFAULT_HOST } = await chrome.storage.sync.get("host"));
  host = host.trim() || DEFAULT_HOST;
  await refresh();
  setInterval(refresh, POLL_MS);
})();
