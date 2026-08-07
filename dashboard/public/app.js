/* Agentic SDLC dashboard — read-only view over /api/state.
   Dependency-free by design: no build step, nothing to maintain but
   this file. The store is the truth; this only renders it. */

"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

/* Board columns: the per-item lifecycle, plus one lane for items a
   human owns. Status colors are reserved for state and always ship
   with an icon + label, never color alone. */
const COLUMNS = [
  { key: "backlog", name: "Backlog", statuses: ["pending"] },
  { key: "review", name: "In review", statuses: ["in_review"] },
  { key: "checks", name: "Verified + preprod", statuses: ["verified", "preprod_passed"] },
  { key: "gate", name: "Awaiting approval", statuses: ["awaiting_approval"] },
  { key: "queued", name: "Queued", statuses: ["queued"] },
  { key: "released", name: "Released", statuses: ["released"] },
  { key: "human", name: "Needs human", statuses: ["escalated", "failed", "rejected"] },
];

const STATUS_FLAG = {
  released: ["🟢", "released", "good"],
  queued: ["🟡", "queued", "warning"],
  awaiting_approval: ["🟡", "awaiting /approve", "warning"],
  escalated: ["🚨", "escalated", "critical"],
  failed: ["❌", "failed", "critical"],
  rejected: ["⛔", "rejected", "serious"],
  in_review: ["🔎", "in review", ""],
  verified: ["🏷️", "verified", ""],
  preprod_passed: ["⚙️", "preprod ✓", ""],
  pending: ["·", "not started", ""],
};

const DECISION_DOT = {
  approve_review: "good", human_approve: "good", merge_pr: "good",
  resolve_incident: "good", create_sprint: "good", open_pr: "good",
  post_dossier: "good",
  reject_pr: "bad", escalate_to_human: "bad", open_incident: "bad",
  hold_merge: "bad",
  escalate_risk_label: "warn", human_override_escalation: "warn",
  refuse_item: "warn", ignore_unauthorized_command: "warn",
};

let state = null;
let prevStatuses = {};
let tokenScope = "sprint";
let tokenTable = false;

/* ---------- helpers ---------- */

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function timeAgo(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return `${Math.max(1, Math.round(s))}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const prUrl = (pr) => state?.repo ? `https://github.com/${state.repo}/pull/${pr}` : null;

/* ---------- data fetch ---------- */

async function refresh() {
  try {
    const r = await fetch("api/state", { cache: "no-store" });
    if (!r.ok) throw new Error(`store returned ${r.status}`);
    state = await r.json();
    $("error-banner").classList.add("hidden");
    $("live-dot").className = "dot ok";
    $("live-text").textContent = "live";
    render();
  } catch (err) {
    $("live-dot").className = "dot bad";
    $("live-text").textContent = "disconnected";
    $("error-banner").textContent =
      `Cannot reach the delivery store (${err.message}) — retrying…`;
    $("error-banner").classList.remove("hidden");
  }
}

/* ---------- render ---------- */

function render() {
  $("project-name").textContent = state.project || "unknown project";
  $("generated-at").textContent =
    `store snapshot ${timeAgo(state.generated_at)}`;
  renderBoard();
  renderEnvs();
  renderIncidents();
  renderTokens();
  prevStatuses = Object.fromEntries(state.items.map((i) => [i.id, i.status]));
}

function renderBoard() {
  const sprint = state.sprint;
  const inSprint = new Set(sprint ? sprint.item_ids : []);
  const items = state.items.filter((i) => inSprint.has(i.id));
  $("sprint-note").textContent = sprint
    ? `sprint #${sprint.id} · ${items.length} items · click a ticket for its history`
    : "no sprint yet — run the orchestrator to assess + pack";

  const board = $("board");
  board.innerHTML = "";
  for (const col of COLUMNS) {
    const colItems = items.filter((i) =>
      col.statuses.includes(i.status || "pending"));
    const el = document.createElement("div");
    el.className = "col";
    el.innerHTML = `<div class="col-head"><span>${col.name}</span>
      <span class="col-count">${colItems.length}</span></div>`;
    for (const item of colItems) el.appendChild(card(item));
    board.appendChild(el);
  }

  const unpacked = state.items.filter((i) => !inSprint.has(i.id));
  $("unpacked").textContent = unpacked.length
    ? `not packed this sprint: ${unpacked.map((i) => i.id).join(", ")}`
    : "";
}

function card(item) {
  const el = document.createElement("div");
  const moved = prevStatuses[item.id] && prevStatuses[item.id] !== item.status;
  el.className = "card" + (moved ? " moving" : "");
  const [icon, label] = STATUS_FLAG[item.status || "pending"] || ["·", item.status];
  const risk = item.claimed_risk
    ? `<span class="chip risk-${esc(item.claimed_risk)}">risk ${esc(item.claimed_risk)}</span>` : "";
  const pr = item.pr
    ? `<span class="chip">PR #${esc(item.pr)}</span>` : "";
  el.innerHTML = `
    <div class="cid">${esc(item.id)}</div>
    <div class="ctitle">${esc(item.title)}</div>
    <div class="cmeta">${pr}${risk}
      <span class="status-flag">${icon} ${esc(label)}</span></div>`;
  el.addEventListener("click", () => openItemDrawer(item));
  return el;
}

/* ---------- environments ---------- */

function latestDeploy(traffic) {
  const list = state.deploys.filter((d) => d.traffic === traffic);
  return list.length ? list[list.length - 1] : null;
}

function renderEnvs() {
  const envs = $("envs");
  envs.innerHTML = "";
  for (const [name, traffic, icon] of
       [["Preprod", "preprod", "⚙️"], ["Production", "100", "🚀"]]) {
    const d = latestDeploy(traffic);
    const el = document.createElement("div");
    el.className = "env";
    el.innerHTML = d ? `
      <div class="ename"><span>${icon} ${name}</span></div>
      <div class="epr">PR #${esc(d.pr)}</div>
      <div class="ewhen">deployed ${timeAgo(d.ts)} · ${esc(d.revision)}</div>
      <div class="earea"><span class="chip">area: ${esc(d.area || "?")}</span></div>`
      : `
      <div class="ename"><span>${icon} ${name}</span></div>
      <div class="epr">—</div>
      <div class="ewhen">no deploys yet</div>`;
    el.addEventListener("click", () => openEnvDrawer(name, traffic));
    envs.appendChild(el);
  }
}

function renderIncidents() {
  const open = state.incidents.filter((i) => i.status === "open");
  const resolved = state.incidents.filter((i) => i.status === "resolved");
  const el = $("incidents");
  el.innerHTML = "";
  if (!open.length) {
    el.innerHTML = `<div class="ok-note">🟢 no open incidents` +
      (resolved.length ? ` · ${resolved.length} resolved` : "") + `</div>`;
  }
  for (const inc of open) {
    const row = document.createElement("div");
    row.className = "inc";
    row.innerHTML = `<span>🚨</span>
      <strong>${esc(inc.area)}</strong>
      <span>error rate ${esc(inc.error_rate)}</span>
      <span class="when">opened ${timeAgo(inc.opened_at)}</span>`;
    el.appendChild(row);
  }
}

/* ---------- token usage (dataviz method: 2 series, legend, direct
   labels, hover tooltip, table view) ---------- */

function renderTokens() {
  const rows = (tokenScope === "sprint"
    ? state.token_usage_sprint : state.token_usage_all)
    .slice().sort((a, b) => b.input_tokens - a.input_tokens);
  const host = $("tokens");
  $("tok-legend").style.display = tokenTable ? "none" : "flex";
  if (!rows.length) {
    host.innerHTML = `<div class="ok-note">no usage recorded yet</div>`;
    return;
  }
  if (tokenTable) { host.innerHTML = tokenTableHtml(rows); return; }

  const max = Math.max(...rows.map((r) => r.input_tokens), 1);
  const rowH = 46, barH = 11, labelW = 118, valueW = 64;
  const width = 460, plotW = width - labelW - valueW;
  const height = rows.length * rowH + 6;
  const x = (v) => (v / max) * plotW;

  /* bar: 4px rounded data-end, square baseline end */
  const bar = (px, y, w, cls) => {
    const r = Math.min(4, w);
    return `<path class="${cls}" d="M${px},${y} h${Math.max(w - r, 0)}
      a${r},${r} 0 0 1 ${r},${r} v${barH - 2 * r} a${r},${r} 0 0 1 -${r},${r}
      h-${Math.max(w - r, 0)} z"></path>`;
  };

  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img"
    aria-label="token usage per agent">`;
  svg += `<style>.b1{fill:var(--series-1)}.b2{fill:var(--series-2)}</style>`;
  rows.forEach((r, i) => {
    const y = i * rowH + 4;
    svg += `<text class="agent-label" x="0" y="${y + 12}">${esc(r.agent)}</text>`;
    svg += `<text class="model-label" x="0" y="${y + 25}">${esc(r.model)}</text>`;
    const g = `data-i="${i}"`;
    svg += `<g class="tok-mark" ${g}>`;
    svg += bar(labelW, y + 4, Math.max(x(r.input_tokens), 2), "b1");
    svg += `<text class="val-label"
      x="${labelW + Math.max(x(r.input_tokens), 2) + 6}" y="${y + 13}">
      ${fmt(r.input_tokens)}</text>`;
    svg += bar(labelW, y + 19, Math.max(x(r.output_tokens), 2), "b2");
    svg += `<text class="val-label"
      x="${labelW + Math.max(x(r.output_tokens), 2) + 6}" y="${y + 28}">
      ${fmt(r.output_tokens)}</text>`;
    svg += `</g>`;
  });
  svg += `</svg>`;
  host.innerHTML = svg;

  host.querySelectorAll(".tok-mark").forEach((g) => {
    const r = rows[Number(g.dataset.i)];
    g.addEventListener("mousemove", (ev) => showTip(ev,
      `<div class="t1">${esc(r.agent)}</div>
       <div class="t2">${esc(r.model)}</div>
       <div>in ${fmt(r.input_tokens)} · out ${fmt(r.output_tokens)}
        · ${fmt(r.calls)} calls</div>`));
    g.addEventListener("mouseleave", hideTip);
  });
}

function tokenTableHtml(rows) {
  const tr = rows.map((r) => `<tr><td>${esc(r.agent)}</td>
    <td>${esc(r.model)}</td><td>${fmt(r.input_tokens)}</td>
    <td>${fmt(r.output_tokens)}</td><td>${fmt(r.calls)}</td></tr>`).join("");
  const ti = rows.reduce((s, r) => s + r.input_tokens, 0);
  const to = rows.reduce((s, r) => s + r.output_tokens, 0);
  return `<table class="tok-table"><thead><tr><th>agent</th><th>model</th>
    <th>input</th><th>output</th><th>calls</th></tr></thead>
    <tbody>${tr}</tbody>
    <tfoot><tr><th>total</th><th></th><th>${fmt(ti)}</th>
    <th>${fmt(to)}</th><th></th></tr></tfoot></table>`;
}

/* ---------- drawers ---------- */

function openDrawer(title, bodyHtml) {
  $("drawer-title").innerHTML = title;
  $("drawer-body").innerHTML = bodyHtml;
  $("drawer").classList.remove("hidden");
  $("scrim").classList.remove("hidden");
}
function closeDrawer() {
  $("drawer").classList.add("hidden");
  $("scrim").classList.add("hidden");
}

function factorsLine(f) {
  const skip = new Set(["pr", "item"]);
  const first = ["rule", "reason_code", "reason", "reasoning",
                 "dominating_rule", "author"];
  const keys = [...first.filter((k) => k in f),
                ...Object.keys(f).filter((k) => !first.includes(k) && !skip.has(k))];
  return keys.slice(0, 5).map((k) => {
    let v = f[k];
    if (typeof v === "object" && v !== null) v = JSON.stringify(v);
    return `${k}=${String(v).slice(0, 120)}`;
  }).join(" · ");
}

function timeline(entries) {
  if (!entries.length) return `<div class="ok-note">no recorded actions</div>`;
  const li = entries.slice().reverse().map((e) => `
    <li><span class="tld ${DECISION_DOT[e.decision] || ""}"></span>
      <div class="who">${esc(e.actor)} · ${timeAgo(e.ts)}</div>
      <div class="what">${esc(e.decision.replaceAll("_", " "))}</div>
      <div class="detail">${esc(factorsLine(e.factors || {}))}</div>
    </li>`).join("");
  return `<ul class="tl">${li}</ul>`;
}

function openItemDrawer(item) {
  const history = state.audit.filter((e) => {
    const f = e.factors || {};
    return f.item === item.id || (item.pr != null && f.pr === item.pr);
  });
  const link = item.pr && prUrl(item.pr)
    ? ` · <a href="${prUrl(item.pr)}" target="_blank" rel="noopener">PR #${item.pr} ↗</a>`
    : (item.pr ? ` · PR #${item.pr}` : "");
  openDrawer(`${esc(item.id)} — action history${link}`, timeline(history));
}

function openEnvDrawer(name, traffic) {
  const list = state.deploys.filter((d) => d.traffic === traffic)
    .slice().reverse();
  const rows = list.map((d) => `
    <li><span class="tld good"></span>
      <div class="who">${timeAgo(d.ts)}</div>
      <div class="what">PR #${esc(d.pr)} → ${esc(d.revision)}</div>
      <div class="detail">area ${esc(d.area || "?")}</div>
    </li>`).join("");
  openDrawer(`${name} — deployment history (${list.length})`,
    list.length ? `<ul class="tl">${rows}</ul>`
                : `<div class="ok-note">no deploys yet</div>`);
}

/* ---------- tooltip ---------- */

function showTip(ev, html) {
  const t = $("tooltip");
  t.innerHTML = html;
  t.classList.remove("hidden");
  const pad = 14;
  const w = t.offsetWidth, winW = window.innerWidth;
  let left = ev.clientX + pad;
  if (left + w > winW - 8) left = ev.clientX - w - pad;
  t.style.left = `${left}px`;
  t.style.top = `${ev.clientY + pad}px`;
}
function hideTip() { $("tooltip").classList.add("hidden"); }

/* ---------- wiring ---------- */

$("drawer-close").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
});
$("tok-sprint").addEventListener("click", () => {
  tokenScope = "sprint"; setSeg(); renderTokens();
});
$("tok-all").addEventListener("click", () => {
  tokenScope = "all"; setSeg(); renderTokens();
});
$("tok-table").addEventListener("click", () => {
  tokenTable = !tokenTable; setSeg(); renderTokens();
});
function setSeg() {
  $("tok-sprint").classList.toggle("active", tokenScope === "sprint");
  $("tok-all").classList.toggle("active", tokenScope === "all");
  $("tok-table").classList.toggle("active", tokenTable);
}

refresh();
setInterval(refresh, POLL_MS);
