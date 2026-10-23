"use strict";
const $ = (id) => document.getElementById(id);
const ask = (msg) => chrome.runtime.sendMessage(msg);

function ago(ms) {
  if (!ms) return "never";
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

async function refresh(fillForm) {
  const r = await ask({ kind: "status" });
  if (!r || !r.ok) return;
  const s = r.status, p = r.page || {};
  $("fwd").textContent = s.forwarded;
  $("queued").textContent = s.queued;
  $("failed").textContent = s.failed + (s.overflowed ? ` (${s.overflowed} from a full queue)` : "");
  $("remembered").textContent = s.remembered;
  $("lastok").textContent = ago(s.lastOkAt);
  $("err").textContent = s.lastError || "";
  $("failedList").innerHTML = "";
  for (const f of s.failedSample || []) {
    const li = document.createElement("li");
    li.textContent = `${f.feed}: ${f.title} (${f.why})`;
    $("failedList").appendChild(li);
  }
  const live = p.at && Date.now() - p.at < 60000;
  $("dot").style.background = !s.webhookSet ? "var(--warn)" : !live ? "var(--muted)" : s.lastError ? "var(--warn)" : "var(--ok)";
  if (!live) {
    $("page").innerHTML = "No Vision tab is reporting. Open <b>app.uxento.io/vision</b> in this Chrome and keep the tab open.";
  } else {
    const rows = (p.panels || []).map((x) => {
      const how = x.mode === "data" ? `${x.count} posts in view` : x.mode === "screen" ? `${x.count} cards (screen fallback)` : "no posts match your filters";
      return `<div class="row"><span>${x.feed.replace(/[<>&]/g, "")}</span><span>${how}</span></div>`;
    }).join("");
    $("page").innerHTML = rows + `<div class="muted">last report ${ago(p.at)}${p.visible === false ? " · tab in background" : ""}</div>`;
  }
  if (fillForm) {
    $("enabled").checked = !!s.enabled;
    $("webhook").placeholder = s.webhookSet ? `saved (ends ${s.webhookHint}) — leave empty to keep` : "https://discord.com/api/webhooks/…";
    $("routes").placeholder = s.routes.length ? `saved for: ${s.routes.join(", ")} — enter all lines again to change`
      : "Fomo Trades = https://discord.com/api/webhooks/…";
  }
}

$("save").onclick = async () => {
  const cfg = { enabled: $("enabled").checked };
  const hook = $("webhook").value.trim();
  if (hook) cfg.webhook = hook;
  const lines = $("routes").value.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length) {
    cfg.routes = {};
    for (const l of lines) {
      const i = l.indexOf("=");
      if (i < 1) { $("msg").className = "msg bad"; $("msg").textContent = `Not "Feed = URL": ${l}`; return; }
      cfg.routes[l.slice(0, i).trim()] = l.slice(i + 1).trim();
    }
  }
  const r = await ask({ kind: "configure", cfg });
  $("msg").className = r.ok ? "msg ok" : "msg bad";
  $("msg").textContent = r.ok ? "Saved." : r.error;
  if (r.ok) { $("webhook").value = ""; $("routes").value = ""; }
  refresh(true);
};
$("test").onclick = async () => {
  $("msg").className = "msg"; $("msg").textContent = "Sending…";
  const r = await ask({ kind: "test" });
  const s = r && r.status;
  $("msg").className = s && !s.lastError ? "msg ok" : "msg bad";
  $("msg").textContent = s && !s.lastError ? "Sent. Check the Discord channel." : (s && s.lastError) || "Failed.";
  refresh(false);
};
$("retry").onclick = async () => {
  const r = await ask({ kind: "retryFailed" });
  $("msg").className = "msg"; $("msg").textContent = `${r.n} post(s) queued again.`;
  refresh(false);
};
refresh(true);
setInterval(() => refresh(false), 2000);
