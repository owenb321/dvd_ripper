"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const STATE_LABEL = {
  IDLE: "idle", DETECTED: "disc detected", DUPLICATE: "duplicate — skipped",
  RIPPING: "ripping", SCREENSHOTS: "screenshots", BUILDING_ISO: "building ISO",
  FINALIZING: "finalizing", EJECTING: "ejecting",
  WAITING_REMOVAL: "remove disc", ERROR: "error", ABORTED: "aborted",
};
const STATE_CLASS = {
  RIPPING: "active", SCREENSHOTS: "active", BUILDING_ISO: "active",
  FINALIZING: "active", DETECTED: "active",
  WAITING_REMOVAL: "good", ERROR: "err", DUPLICATE: "err", ABORTED: "warn",
};

const gb = (b) => (b / 1073741824).toFixed(b > 10737418240 ? 0 : 1) + " GB";
const mins = (s) => s == null ? "" : Math.round(s / 60) + " min";
// minutes once it is worth saying in minutes (test thresholds are seconds)
const dur = (s) => s < 60 ? `${Math.round(s)}s` : `${Math.round(s / 60)} min`;
const eta = (s) => {
  if (s == null) return "";
  if (s < 90) return s + "s left";
  return Math.round(s / 60) + " min left";
};

let lastEventTime = null;
// device -> name typed into the box but not saved yet. The drives section is
// re-rendered wholesale every poll, so unsaved text has to live out here.
const pendingNames = {};

async function poll() {
  try {
    const st = await (await fetch("/api/status")).json();
    renderDisk(st.disk);
    renderNotify(st.notify);
    renderDrives(st.drives);
    renderBackfill(st.backfill);
    renderEvents(st.events);
    // reload history when something that changes it happens
    const newest = st.events.find((e) =>
      ["rip_done", "rename", "backfill", "mock"].includes(e.kind));
    if (newest && newest.time !== lastEventTime) {
      lastEventTime = newest.time;
      loadHistory();
      loadCensus();
    }
  } catch (e) { /* server restarting — keep polling */ }
  setTimeout(poll, 2000);
}

function renderDisk(disk) {
  if (!disk) return;
  const cls = disk.space_low ? "low" : "";
  $("disk").innerHTML =
    `<span class="${cls}">output free: ${gb(disk.output_free_bytes)}</span>` +
    `<span class="${cls}">scratch free: ${gb(disk.work_free_bytes)}</span>` +
    (disk.space_low ? `<span class="low">⚠️ disk space low</span>` : "");
}

// Notifications row: without this a broken webhook is indistinguishable from
// "nothing has happened yet" — the last attempt's real outcome is shown.
function renderNotify(n) {
  const el = $("notify");
  if (!n) return;
  if (!n.configured) {
    el.innerHTML = `<span class="muted">Discord: not configured ` +
      `(set DISCORD_WEBHOOK_URL)</span>`;
    return;
  }
  const last = n.last;
  const when = last ? esc(last.time.slice(11, 19)) : "";
  const state = !last
    ? `<span class="muted">Discord: configured — nothing sent yet</span>`
    : last.ok === true
      ? `<span class="ok">Discord: last send OK</span> ` +
        `<span class="muted">(${esc(last.event)}, ${when})</span>`
      : last.ok === false
        ? `<span class="low" title="${esc(last.detail)}">Discord: last send FAILED — ` +
          `${esc(last.detail.slice(0, 110))}${last.detail.length > 110 ? "…" : ""}</span>`
        // ok === null: filtered by NOTIFY_EVENTS / not configured — not a fault
        : `<span class="muted">Discord: ${esc(last.event)} ${esc(last.detail)}</span>`;
  el.innerHTML = state +
    ` <button id="notify-test">Test notification</button>`;
}

function renderDrives(drives) {
  const el = $("drives");
  if (!drives.length) {
    el.innerHTML = `<div class="card subtle">No drives configured.</div>`;
    return;
  }
  // never re-render out from under someone typing in a name box (it would
  // drop the caret, and on a phone the keyboard with it)
  if (el.contains(document.activeElement) &&
      document.activeElement.matches("input[data-dev]")) return;
  el.innerHTML = drives.map((d) => {
    const pct = d.progress != null ? Math.round(d.progress * 100) : null;
    const busy = ["RIPPING", "BUILDING_ISO"].includes(d.state);
    let html = `<div class="card drive">
      <div class="top">
        <span class="dev">${esc(d.device)}</span>
        <span class="pill ${STATE_CLASS[d.state] || ""}">${esc(STATE_LABEL[d.state] || d.state)}</span>
      </div>`;
    if (d.label) html += `<div class="label">${esc(d.label)}${d.disc_bytes ? " · " + gb(d.disc_bytes) : ""}</div>`;
    if (busy && pct != null) {
      // Past the estimate (see drive.py: the disc's declared size and the
      // mirror byte count are both estimates the real work can exceed) the
      // percentage is stuck at 100 and would read as "finished but hanging".
      // Show what is actually known — bytes written — instead of the lie.
      const left = d.overrun
        ? `${esc(d.phase || "")} · ${gb(d.bytes_done)} written`
        : `${esc(d.phase || "")} ${pct}%`;
      html += `<div class="meter ${d.overrun ? "indet" : ""}" role="progressbar" aria-valuenow="${pct}"><div style="width:${pct}%"></div></div>
        <div class="prog-line">
          <span>${left}</span>
          <span>${[d.speed_mbps ? d.speed_mbps.toFixed(1) + " MB/s" : "",
                    eta(d.eta_s)].filter(Boolean).join(" · ")}</span>
        </div>`;
    }
    if (d.nameable) {
      const val = pendingNames[d.device] ?? d.queued_name ?? "";
      html += `<div class="namebox">
        <input type="text" data-dev="${esc(d.device)}" value="${esc(val)}"
               placeholder="Movie name" autocapitalize="words"
               spellcheck="false" enterkeyhint="done"
               aria-label="name for this rip">
        <button class="save" data-dev="${esc(d.device)}">Save name</button>
      </div>
      <div class="named">${d.queued_name
        ? `✓ will finish as <b>${esc(d.planned_name)}.iso</b>`
        : `type the real title while it rips — it's applied when the rip finishes`}</div>`;
    } else {
      delete pendingNames[d.device];   // cycle over: don't carry text to the next disc
    }
    // give-up button: a dying disc crawls at ~0 MB/s instead of failing
    if (d.abortable || d.aborting) {
      html += `<div class="abortbox">
        <button class="abort" data-dev="${esc(d.device)}"
                ${d.aborting ? "disabled" : ""}>${d.aborting
                  ? "aborting…" : "Abort &amp; eject"}</button>
        <span class="hint">${d.aborting
          ? "stopping — the disc ejects when the tools exit"
          : "gives up on this disc — no ISO is kept"}</span>
      </div>`;
    }
    if (d.stalled && !d.aborting) {
      html += `<div class="callout dup">🐌 Stalled — no progress for ` +
        `${dur(d.stalled_s)}` +
        (d.stall_abort_in_s != null
          ? ` · auto-abort in ${dur(Math.max(0, d.stall_abort_in_s))}`
          : ``) + `</div>`;
    }
    if (d.duplicate_of) {
      html += `<div class="callout dup">♻️ Already ripped as <b>${esc(d.duplicate_of)}</b></div>`;
    }
    if (d.error) {
      // an abort is a deliberate act, not a failure — don't shout about it.
      // `aborted` outlives the ABORTED state (the card goes back to idle once
      // the disc is out, but the message stays until the next disc).
      html += `<div class="callout ${d.aborted ? "dup" : "error"}">` +
        `${d.aborted ? "🛑" : "❌"} ${esc(d.error)}` +
        (d.error_detail ? `<pre>${esc(d.error_detail)}</pre>` : "") + `</div>`;
    }
    return html + `</div>`;
  }).join("");
}

function renderBackfill(b) {
  const box = $("backfill-box");
  if (!b || b.state !== "running") { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("backfill").textContent =
    `📚 Backfilling sidecars: ${b.done}/${b.total}` +
    (b.current ? ` — ${b.current}` : "");
}

function renderEvents(events) {
  $("events").innerHTML = events.map((e) =>
    `<li class="${e.kind === "error" ? "err" : ""}">` +
    `<span class="t">${esc(e.time.slice(11, 19))}</span>` +
    `${esc(e.device)}: ${esc(e.message)}</li>`).join("");
}

// A rip that lost libdvdcss still writes a full-size, well-formed ISO, so the
// only place the failure can surface is here. "Unverified" is deliberately a
// separate, quieter state from "encrypted": one is a known-bad artifact, the
// other is an absence of evidence, and calling them the same thing would train
// people to ignore both.
function cssWarn(css) {
  if (!css || css.clean === true) return "";
  if (css.clean === false) {
    return `<div class="callout error">⚠ CSS-encrypted — this ISO will not play`
      + ` (${css.scrambled_pct}% of ${css.packs_checked} sampled packs).`
      + ` Check that libdvdcss is installed, then re-rip.</div>`;
  }
  return `<div class="sub">decryption unverified: ${esc(css.error || "scan did not run")}</div>`;
}

async function loadHistory() {
  const data = await (await fetch("/api/history")).json();
  const el = $("history");
  if (!data.rips.length) {
    el.innerHTML = `<div class="card subtle">No rips yet — insert a disc.</div>`;
    return;
  }
  el.innerHTML = data.rips.map((r) => {
    const menu = r.screenshots.find((s) => s.endsWith(".menu.jpg"));
    const titles = r.screenshots.filter((s) => !s.endsWith(".menu.jpg"));
    const meta = r.meta || {};
    const failed = meta.status && meta.status !== "ok";
    const sub = [
      r.finished_at ? r.finished_at.slice(0, 10) : "",
      gb(r.iso_bytes),
      meta.duration_s != null ? mins(meta.duration_s) : (meta.backfilled ? "backfilled" : ""),
      r.census_summary ? `${r.census_summary.n_titles} titles · ${r.census_summary.max_chapters} ch` : "",
    ].filter(Boolean).join(" · ");
    return `<div class="card rip" data-name="${esc(r.name)}">
      ${menu
        ? `<img class="thumb" loading="lazy" src="/media/${encodeURIComponent(menu)}" alt="menu" onclick="showImg(this.src)">`
        : `<div class="noimg">📀</div>`}
      <div class="body">
        <div class="name">${esc(r.name)}</div>
        <div class="sub">${esc(sub)}</div>
        ${failed ? `<div class="callout error">❌ ${esc(meta.status)}</div>` : ""}
        ${cssWarn(meta.css)}
        ${r.census_error ? `<div class="sub">census: ${esc(r.census_error)}</div>` : ""}
        <div class="chips">${r.census_chips.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div>
        ${titles.length ? `<div class="shots">${titles.map((s) =>
          `<img loading="lazy" src="/media/${encodeURIComponent(s)}" alt="" onclick="showImg(this.src)">`).join("")}</div>` : ""}
        <div class="actions"><button onclick="renameRip('${esc(r.name)}')">rename</button></div>
      </div>
    </div>`;
  }).join("");
}

async function loadCensus() {
  const data = await (await fetch("/api/census/aggregate")).json();
  if (!data.n_discs) { $("census").innerHTML = ""; return; }
  const max = Math.max(...data.rows.map((r) => r.count), 1);
  $("census").innerHTML = `<table><tbody>` + data.rows
    .filter((r) => r.count > 0)
    .map((r) => {
      const novel = r.count === 1;
      return `<tr>
        <td>${novel ? `<span class="novel">${esc(r.label)}</span>` : esc(r.label)}
          ${novel ? `<div class="discs">${esc(r.discs[0])}</div>` : ""}</td>
        <td class="n">${r.count}/${data.n_discs}</td>
        <td style="width:30%"><div class="bar" style="width:${Math.round(100 * r.count / max)}%"></div></td>
      </tr>`;
    }).join("") + `</tbody></table>`;
}

async function renameRip(name) {
  const newName = prompt("New name for " + name, name);
  if (!newName || newName === name) return;
  const res = await fetch("/api/rename", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name, new_name: newName}),
  });
  const out = await res.json();
  if (out.error) alert(out.error);
  else { loadHistory(); loadCensus(); }
}

async function saveName(device, name) {
  const res = await fetch("/api/name", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device, name}),
  });
  const out = await res.json();
  if (out.error) { alert(out.error); return; }
  delete pendingNames[device];           // the server's copy is authoritative now
  if (document.activeElement) document.activeElement.blur();
}

$("drives").addEventListener("input", (e) => {
  const inp = e.target.closest("input[data-dev]");
  if (inp) pendingNames[inp.dataset.dev] = inp.value;
});
$("drives").addEventListener("keydown", (e) => {
  const inp = e.target.closest("input[data-dev]");
  if (inp && e.key === "Enter") saveName(inp.dataset.dev, inp.value);
});
async function abortRip(device) {
  if (!confirm(`Abort the rip in ${device} and eject the disc?\n\n` +
               `Nothing is kept — no ISO, no screenshots.`)) return;
  const res = await fetch("/api/abort", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device}),
  });
  const out = await res.json();
  if (out.error) alert(out.error);
}

$("drives").addEventListener("click", (e) => {
  const save = e.target.closest("button.save");
  if (save) {
    const inp = $("drives").querySelector(
      `input[data-dev="${CSS.escape(save.dataset.dev)}"]`);
    saveName(save.dataset.dev, inp ? inp.value : "");
    return;
  }
  const abort = e.target.closest("button.abort");
  if (abort) abortRip(abort.dataset.dev);
});

function showImg(src) {
  $("lightbox-img").src = src;
  $("lightbox").classList.remove("hidden");
}
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));
$("backfill-btn").addEventListener("click", async () => {
  await fetch("/api/backfill", {method: "POST"});
});
$("notify").addEventListener("click", async (e) => {
  const btn = e.target.closest("#notify-test");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "sending…";
  const out = await (await fetch("/api/notify/test", {method: "POST"})).json();
  alert(out.ok ? "Sent — check Discord. (" + out.detail + ")"
               : "Failed: " + out.detail);
});

loadHistory();
loadCensus();
poll();
