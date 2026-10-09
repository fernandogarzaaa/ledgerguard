"use strict";
const $ = (id) => document.getElementById(id);
let current = null;
let lastApproval = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function boot() {
  const h = await api("/health");
  $("status").textContent = `Airwallex: ${h.airwallex_mode} · analyst: ${h.llm} · v${h.version}`;
  $("pin").hidden = !h.approval_pin_required;
  if (h.airwallex_mode === "mock") $("mocktools").hidden = false;
  const presets = await api("/api/presets");
  for (const p of presets) {
    const b = el("button", "chip", p.label);
    b.onclick = () => { $("text").value = p.text; investigate(); };
    $("presets").appendChild(b);
  }
}

async function investigate() {
  const text = $("text").value.trim();
  if (!text) return;
  $("go").disabled = true;
  $("go").textContent = "Investigating…";
  lastApproval = null;
  try {
    const inc = await api("/api/incidents", { method: "POST", body: { text, use_llm: $("useLlm").checked } });
    render(inc);
  } catch (e) {
    alert(e.message);
  } finally {
    $("go").disabled = false;
    $("go").textContent = "Investigate";
  }
}

function render(inc) {
  current = inc;
  for (const id of ["decisionCard", "timelineCard", "outputsCard", "traceCard"]) $(id).hidden = false;
  const inv = inc.invoice;
  $("invoiceLine").textContent = inv ? `${inv.invoice_id} · ${inv.amount} · ${inv.supplier}` : inc.invoice_id;
  const tl = $("timeline");
  tl.innerHTML = "";
  for (const e of inc.timeline) {
    const li = el("li", `ev ${e.severity} k-${e.kind}`);
    li.appendChild(el("time", "", e.at.slice(0, 16).replace("T", " ") + " UTC"));
    li.appendChild(el("div", "t", e.title));
    if (e.detail) li.appendChild(el("div", "d", e.detail));
    tl.appendChild(li);
  }
  const c = inc.classification, a = inc.action;
  $("case").textContent = c.case;
  $("case").className = `pill c-${c.case}`;
  $("actionTitle").textContent = a.title;
  $("rationale").textContent = a.rationale;
  $("cost").textContent = a.cost_note;
  $("risk").textContent = `${a.risk_level.toUpperCase()} · ${a.risk_note}`;
  $("binding").textContent = a.moves_money
    ? (a.kind === "CANCEL_DUPLICATE"
      ? `cancel ${a.target_transfer_id}`
      : `${a.amount} ${a.currency} ← ${a.source_currency} · ${a.beneficiary_id} · ${a.transfer_method} · request_id ${a.request_id}`)
    : "no money moves";
  const ev = $("evidence");
  ev.innerHTML = "";
  for (const x of a.evidence) {
    const li = el("li");
    li.appendChild(el("code", "", x.ref));
    li.appendChild(document.createTextNode(" " + x.claim));
    ev.appendChild(li);
  }
  if (c.flags.length) {
    const li = el("li", "flags", "flags: " + c.flags.join(", "));
    ev.appendChild(li);
  }
  $("approveBtn").textContent = a.moves_money ? "Approve & execute" : "Approve & record";
  $("approveBtn").disabled = inc.status !== "awaiting_approval" && inc.status !== "stale";
  $("replayBtn").hidden = !lastApproval;
  showExec(inc.execution);
  $("supplier").textContent = inc.supplier_message;
  $("report").textContent = inc.report_markdown || "";
  $("reportLink").href = `/api/incidents/${inc.id}/report.md`;
  $("model").textContent = `${inc.model}${inc.fallback_used ? " (fallback)" : ""}`;
  const tr = $("trace");
  tr.innerHTML = "";
  for (const s of inc.trace) {
    const li = el("li", `tr a-${s.actor}`);
    let line = `[${s.actor}] `;
    if (s.actor === "model") {
      line += s.tool_calls.length ? s.tool_calls.map((t) => `${t.name}(${JSON.stringify(t.args)})`).join(", ") : (s.content || "(no tool call)");
    } else if (s.actor === "tool") {
      line += `${s.name} → ${s.result}`;
    } else {
      line += s.event + (s.case ? ` · ${s.case} → ${s.action}` : "");
    }
    if (s.ms !== undefined) line += `  (${s.ms} ms)`;
    li.textContent = line;
    tr.appendChild(li);
  }
}

function showExec(x) {
  const box = $("execResult");
  if (!x) { box.hidden = true; return; }
  box.hidden = false;
  box.className = `exec ${x.outcome === "refused" ? "bad" : "ok"}`;
  box.textContent = `${x.outcome}: ${x.message}`;
}

async function approveAndExecute() {
  if (!current) return;
  const approver = $("approver").value.trim();
  if (!approver) { $("approver").focus(); return; }
  $("approveBtn").disabled = true;
  try {
    const appr = await api(`/api/incidents/${current.id}/approve`, { method: "POST", body: { approver, pin: $("pin").value } });
    lastApproval = appr;
    const res = await api(`/api/incidents/${current.id}/execute`, { method: "POST", body: { approval_id: appr.approval_id, approval_token: appr.approval_token } });
    render(res.incident);
  } catch (e) {
    alert(e.message);
    $("approveBtn").disabled = false;
  }
}

async function replay() {
  if (!current || !lastApproval) return;
  const res = await api(`/api/incidents/${current.id}/execute`, { method: "POST", body: { approval_id: lastApproval.approval_id, approval_token: lastApproval.approval_token } });
  render(res.incident);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-copy]");
  if (t) navigator.clipboard.writeText($(t.dataset.copy).textContent);
});
$("go").onclick = investigate;
$("approveBtn").onclick = approveAndExecute;
$("replayBtn").onclick = replay;
$("reset").onclick = async () => { await api("/api/mock/reset", { method: "POST" }); location.reload(); };
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) investigate(); });
boot();
