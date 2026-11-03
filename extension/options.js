const $ = (id) => document.getElementById(id);

chrome.storage.sync.get("host").then(({ host }) => {
  $("host").value = host || "10.0.0.162";
});

$("save").onclick = async () => {
  await chrome.storage.sync.set({ host: $("host").value.trim() });
  $("msg").textContent = "Saved";
};

$("test").onclick = async () => {
  $("msg").textContent = "Testing...";
  try {
    const res = await fetch(`http://${$("host").value.trim()}:8888/api/state`);
    const s = await res.json();
    $("msg").textContent = `Connected: Playlet ${s.app.app_version}`;
  } catch {
    $("msg").textContent = "Can't reach Playlet (is it open on the Roku?)";
  }
};
