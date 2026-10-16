/* Supplier Quality Copilot web app. No build step: plain ES2020 in the browser.
 *
 * Sign-in: Cognito managed login, authorization code + PKCE. Tokens live in
 * sessionStorage for this tab. API calls send the ID token to API Gateway's
 * Cognito authorizer.
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = {
  config: null,
  user: { email: "", isReviewer: false },
  sessionId: sessionStorage.getItem("sqc.session") || "",
  thread: [],              // [{role:'user'|'bot', ...}]
  uploads: JSON.parse(sessionStorage.getItem("sqc.uploads") || "[]"),
  selectedScar: null,
  timers: new Set(),
};

// ------------------------------------------------------------------ utils
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* Small markdown renderer for agent answers and SCAR drafts: headings, paragraphs,
 * lists, tables, code, bold, italic, links. Every line is HTML-escaped before any
 * markup is added, so model output can't inject HTML. No CDN dependency. */
function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function md(text) {
  const lines = esc(String(text || "").replace(/\r\n/g, "\n")).split("\n");
  const out = [];
  let i = 0;
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => mdInline(c.trim()));
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const buf = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) buf.push(lines[i]);
      i++;
      out.push(`<pre><code>${buf.join("\n")}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push(`<h3>${mdInline(h[2])}</h3>`); i++; continue; }
    if (isTableRow(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && isTableRow(lines[i]); i++) rows.push(cells(lines[i]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const list = line.match(/^\s*([-*+]|\d+[.)])\s+/);
    if (list) {
      const ordered = /\d/.test(list[1]);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        items.push(mdInline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "")));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((t) => `<li>${t}</li>`).join("")}</${tag}>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])
           && !isTableRow(lines[i])) {
      para.push(mdInline(lines[i]));
      i++;
    }
    out.push(`<p>${para.join("<br>")}</p>`);
  }
  return out.join("");
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.remove("show"), 3200);
}

function statusTag(status) {
  const map = {
    PENDING_REVIEW: ["hold", "Pending review"],
    APPROVED: ["accept", "Approved"],
    REJECTED: ["reject", "Rejected"],
  };
  const [cls, label] = map[status] || ["", status || "Unknown"];
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}

function ncrChip(id) {
  return `<button class="chip" type="button" data-ncr="${esc(id)}">${esc(id)}</button>`;
}

function stopTimers() {
  state.timers.forEach((t) => clearTimeout(t));
  state.timers.clear();
}

// ------------------------------------------------------------------ auth
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function randomString(len = 64) {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  return b64url(bytes).slice(0, len);
}

async function pkceChallenge(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(digest);
}

function decodeJwt(token) {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(escape(atob(part))));
  } catch {
    return {};
  }
}

function saveTokens(t) {
  const claims = decodeJwt(t.id_token);
  sessionStorage.setItem("sqc.tokens", JSON.stringify({
    id: t.id_token,
    refresh: t.refresh_token || JSON.parse(sessionStorage.getItem("sqc.tokens") || "{}").refresh,
    exp: (claims.exp || 0) * 1000,
  }));
}

function tokens() {
  return JSON.parse(sessionStorage.getItem("sqc.tokens") || "null");
}

async function tokenRequest(params) {
  const res = await fetch(`${state.config.authDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: state.config.userPoolClientId, ...params }),
  });
  if (!res.ok) throw new Error(`Sign-in failed (${res.status})`);
  return res.json();
}

async function signIn() {
  const verifier = randomString(64);
  const st = randomString(24);
  sessionStorage.setItem("sqc.pkce", JSON.stringify({ verifier, state: st }));
  const q = new URLSearchParams({
    response_type: "code",
    client_id: state.config.userPoolClientId,
    redirect_uri: state.config.redirectUri,
    scope: "openid email profile",
    code_challenge_method: "S256",
    code_challenge: await pkceChallenge(verifier),
    state: st,
  });
  location.assign(`${state.config.authDomain}/oauth2/authorize?${q}`);
}

function signOut() {
  sessionStorage.clear();
  const q = new URLSearchParams({
    client_id: state.config.userPoolClientId,
    logout_uri: state.config.redirectUri,
  });
  location.assign(`${state.config.authDomain}/logout?${q}`);
}

async function completeSignIn() {
  const url = new URL(location.href);
  const code = url.searchParams.get("code");
  if (!code) return false;
  const saved = JSON.parse(sessionStorage.getItem("sqc.pkce") || "{}");
  history.replaceState(null, "", url.pathname + (url.hash || "#/ask"));
  if (!saved.verifier || saved.state !== url.searchParams.get("state")) {
    throw new Error("Sign-in response didn't match this tab. Sign in again.");
  }
  sessionStorage.removeItem("sqc.pkce");
  saveTokens(await tokenRequest({
    grant_type: "authorization_code",
    code,
    redirect_uri: state.config.redirectUri,
    code_verifier: saved.verifier,
  }));
  return true;
}

async function idToken() {
  const t = tokens();
  if (t && t.exp - Date.now() > 60_000) return t.id;
  if (t && t.refresh) {
    try {
      saveTokens(await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh }));
      return tokens().id;
    } catch { /* fall through to sign-in */ }
  }
  return null;
}

// ------------------------------------------------------------------ API
async function api(path, { method = "GET", body } = {}) {
  const token = await idToken();
  if (!token) { showGate("Your session ended. Sign in again to continue."); throw new Error("signed out"); }
  const res = await fetch(state.config.apiUrl + path.replace(/^\//, ""), {
    method,
    headers: { Authorization: token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) {
    showGate("Your session ended. Sign in again to continue.");
    throw new Error("signed out");
  }
  if (!res.ok) {
    const err = new Error(`${data.error || data.message || "Request failed"} (HTTP ${res.status} on ${method} /${path.replace(/^\//, "").split("?")[0]})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// ------------------------------------------------------------------ shell
function showGate(message) {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  if (message) $("#gate-msg").textContent = message;
}

function setCount(id, n) {
  const el = $(id);
  el.hidden = !n;
  el.textContent = n || "";
}

async function refreshCounts() {
  try {
    const [alerts, scars] = await Promise.all([api("alerts"), api("scars")]);
    setCount("#alert-count", alerts.alerts.length);
    setCount("#scar-count", scars.scars.filter((s) => s.status === "PENDING_REVIEW").length);
  } catch { /* counts are a convenience */ }
}

const VIEWS = { ask: renderAsk, alerts: renderAlerts, upload: renderUpload, scars: renderScars };

function route() {
  stopTimers();
  const [name, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const view = VIEWS[name] ? name : "ask";
  document.querySelectorAll(".nav a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  VIEWS[view](rest.map(decodeURIComponent));
  $("#view").focus({ preventScroll: true });
}

// ------------------------------------------------------------------ NCR drawer
async function openNcr(ncrId) {
  const drawer = $("#drawer");
  $("#drawer-title").textContent = ncrId;
  $("#drawer-body").innerHTML = `<p class="meta-line">Loading ${esc(ncrId)}…</p>`;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#drawer-close").focus();
  try {
    const { record, text } = await api(`ncrs/${encodeURIComponent(ncrId)}`);
    const rows = [
      ["Supplier", `${record.supplier_name} (${record.supplier_id})`],
      ["Part", `${record.part_number} ${record.part_description || ""}`],
      ["Program", record.program],
      ["Detected", `${record.detected_at}, ${record.detection_point || ""}`],
      ["Defect", `${record.defect_code}: ${record.defect_description}`],
      ["Quantity", record.quantity_affected],
      ["Root cause", `${record.root_cause_category}: ${record.root_cause_detail}`],
      ["Containment", record.containment],
      ["Corrective action", record.corrective_action],
      ["Disposition", record.disposition],
    ];
    const sev = record.severity === "critical" ? "reject" : record.severity === "major" ? "hold" : "";
    const st = record.status === "closed" ? "accept" : "hold";
    $("#drawer-body").innerHTML = `
      <div class="chips"><span class="tag ${sev}">${esc(record.severity || "severity n/a")}</span>
        <span class="tag ${st}">${esc(record.status || "")}</span></div>
      <dl class="record">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      <div><h3>Source document</h3><p class="meta-line">As received, before extraction</p>
        <pre class="source">${esc(text || "Source text not available.")}</pre></div>
      <button class="btn" type="button" data-ask-ncr="${esc(ncrId)}">Ask about this NCR</button>`;
  } catch (err) {
    $("#drawer-body").innerHTML = `<p>${esc(err.message)}</p>`;
  }
}

function closeDrawer() {
  const drawer = $("#drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

// ------------------------------------------------------------------ Ask
const STARTERS = [
  "Which suppliers had the most critical NCRs in the last 90 days?",
  "Are there any repeat root causes across suppliers right now?",
  "What worked last time contacts came in with thin gold plating?",
  "Summarize open NCRs for Ardent Interconnect and what's still missing from their investigation.",
];

const TOOL_LABELS = {
  query_quality_data: "Queried quality data",
  get_repeat_root_cause_alerts: "Checked cross-supplier patterns",
  find_similar_ncrs: "Looked for similar NCRs at other suppliers",
  save_scar_draft: "Saved a SCAR draft for review",
};

function renderStep(step) {
  switch (step.kind) {
    case "thinking":
      return `<span class="thinking">${esc(step.text)}</span>`;
    case "tool": {
      const label = TOOL_LABELS[step.name] || step.name;
      const input = step.input || {};
      if (step.name === "query_quality_data") return `${esc(label)}<pre>${esc(input.sql)}</pre>`;
      if (step.name === "find_similar_ncrs") return `${esc(label)} for ${esc(input.ncr_id)}`;
      if (step.name === "save_scar_draft") return `${esc(label)}: ${esc(input.title)}`;
      return esc(label);
    }
    case "tool_result":
      return `<details><summary>Result</summary><pre>${esc(step.text)}</pre></details>`;
    case "kb_search":
      return `Searched NCR and 8D documents for “${esc(step.query)}”`;
    case "kb_result":
      return `Found ${step.count} passage${step.count === 1 ? "" : "s"}
        ${step.ncr_ids?.length ? `<div class="chips">${step.ncr_ids.map(ncrChip).join("")}</div>` : ""}`;
    case "guardrail":
      return "Guardrail intervened";
    default:
      return esc(JSON.stringify(step));
  }
}

function botMessage(m) {
  if (m.status === "pending") {
    return `<div class="msg-bot pending"><span class="pulse"></span>
      <span>Working on it${m.elapsed ? `, ${m.elapsed}s` : ""}. Multi-step questions can take up to a minute.</span></div>`;
  }
  if (m.status === "error") return `<div class="msg-bot error">${esc(m.error)}</div>`;
  const cites = (m.citations || []).filter((c) => c.ncr_id);
  const steps = m.steps || [];
  return `<div class="msg-bot">
    <div class="answer">${md(m.answer || "No answer returned.")}</div>
    ${cites.length ? `<div class="sources"><h4>Sources</h4><div class="chips">${cites.map((c) => ncrChip(c.ncr_id)).join("")}</div></div>` : ""}
    ${steps.length ? `<details class="steps"><summary>How I got this answer (${steps.length} steps${m.elapsedMs ? `, ${(m.elapsedMs / 1000).toFixed(0)}s` : ""})</summary>
      <ol>${steps.map((s) => `<li><div>${renderStep(s)}</div></li>`).join("")}</ol></details>` : ""}
  </div>`;
}

function drawThread() {
  const thread = $("#thread");
  if (!thread) return;
  if (!state.thread.length) {
    thread.innerHTML = `<div class="starters"><h2>What do you want to know?</h2>
      <p class="meta-line">Answers cite the NCRs they came from. Try one of these:</p>
      ${STARTERS.map((q) => `<button type="button" data-starter="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
    return;
  }
  thread.innerHTML = state.thread.map((m) => m.role === "user"
    ? `<div class="msg-user">${esc(m.text)}</div>` : botMessage(m)).join("");
  window.scrollTo({ top: document.body.scrollHeight });
}

function renderAsk(args) {
  $("#view").innerHTML = `
    <div class="view-head"><div><h1>Ask</h1>
      <p>Questions about NCRs, suppliers and root causes. The copilot queries the data, reads the documents, and shows its work.</p></div>
      <button class="btn small" type="button" id="new-chat">New conversation</button></div>
    <section class="ask">
      <div class="thread" id="thread" aria-live="polite"></div>
      <form class="composer" id="composer">
        <textarea id="question" rows="2" aria-label="Question" placeholder="Ask about a supplier, an NCR, or a defect…" maxlength="4000"></textarea>
        <button class="btn primary" type="submit" id="send">Ask</button>
        <div class="composer-row"><span>Enter to send, Shift+Enter for a new line</span><span>Synthetic data</span></div>
      </form>
    </section>`;
  drawThread();
  const prefill = sessionStorage.getItem("sqc.prefill");
  if (prefill) {
    sessionStorage.removeItem("sqc.prefill");
    $("#question").value = prefill;
  }
  if (args[0] === "send") {
    history.replaceState(null, "", "#/ask");
    if (prefill) ask(prefill);
  }
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#question").value.trim();
    if (q) ask(q);
  });
  $("#new-chat").addEventListener("click", () => {
    state.thread = [];
    state.sessionId = "";
    sessionStorage.removeItem("sqc.session");
    drawThread();
    $("#question").focus();
  });
}

function askAbout(question) {
  sessionStorage.setItem("sqc.prefill", question);
  closeDrawer();
  if (location.hash === "#/ask/send") route();
  else location.hash = "#/ask/send";
}

async function ask(question) {
  const input = $("#question");
  if (input) input.value = "";
  state.thread.push({ role: "user", text: question });
  const bot = { role: "bot", status: "pending", elapsed: 0 };
  state.thread.push(bot);
  drawThread();
  const send = $("#send");
  if (send) send.disabled = true;
  const started = Date.now();
  try {
    const job = await api("chat", { method: "POST", body: { question, sessionId: state.sessionId } });
    state.sessionId = job.sessionId;
    sessionStorage.setItem("sqc.session", job.sessionId);
    let misses = 0;
    for (;;) {
      await sleep(2000);
      let r;
      try {
        r = await api(`chat/${job.jobId}`);
        misses = 0;
      } catch (err) {
        // Ride out brief network or gateway errors while the agent keeps working.
        if (err.message === "signed out" || ++misses > 3) throw err;
        continue;
      }
      bot.elapsed = Math.round((Date.now() - started) / 1000);
      if (r.status === "DONE") { Object.assign(bot, r, { status: "done" }); break; }
      if (r.status === "ERROR") { Object.assign(bot, { status: "error", error: r.error }); break; }
      if (bot.elapsed > 300) { Object.assign(bot, { status: "error", error: "No answer after 5 minutes. Try a narrower question." }); break; }
      drawThread();
    }
  } catch (err) {
    Object.assign(bot, { status: "error", error: err.message });
  }
  drawThread();
  const s = $("#send");
  if (s) s.disabled = false;
  refreshCounts();
}

// ------------------------------------------------------------------ Patterns
async function renderAlerts() {
  $("#view").innerHTML = `
    <div class="view-head"><div><h1>Cross-supplier patterns</h1>
      <p>NCRs at different suppliers that share a failure mechanism, grouped by the detector and explained by the copilot.</p>
      <p class="meta-line" id="alerts-updated"></p></div>
      <button class="btn primary" type="button" id="run-detector">Run detector now</button></div>
    <section class="alerts" id="alerts"><p class="meta-line">Loading patterns…</p></section>`;
  $("#run-detector").addEventListener("click", runDetector);
  await drawAlerts();
}

async function drawAlerts() {
  const data = await api("alerts").catch((err) => ({ error: err.message }));
  const box = $("#alerts");
  if (!box) return data;
  if (data.error) { box.innerHTML = `<div class="empty">${esc(data.error)}</div>`; return data; }
  $("#alerts-updated").textContent = data.updatedAt ? `Last run ${fmtDate(data.updatedAt)}` : "The detector hasn't run yet.";
  setCount("#alert-count", data.alerts.length);
  if (!data.alerts.length) {
    box.innerHTML = `<div class="empty"><strong>No active cross-supplier patterns</strong>
      New NCRs are compared against every supplier's history. Run the detector after uploading an NCR.</div>`;
    return data;
  }
  box.innerHTML = data.alerts.map((a) => `
    <article class="alert">
      <div class="alert-stub" aria-label="${a.supplier_count} suppliers affected">
        <b>${a.supplier_count}</b><span>suppliers</span><span>${a.ncr_count} NCRs</span>
      </div>
      <div class="alert-body">
        <h2>${esc(a.pattern_label)}</h2>
        <p>${esc(a.summary)}</p>
        <dl class="alert-facts">
          <div class="fact"><dt>Suppliers</dt><dd class="chips">${a.supplier_ids.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</dd></div>
          <div class="fact"><dt>NCRs</dt><dd class="chips">${a.ncr_ids.map(ncrChip).join("")}</dd></div>
          <div class="fact"><dt>Dominant root cause</dt><dd>${esc(a.dominant_root_cause_category)}</dd></div>
          <div class="fact"><dt>Average similarity</dt><dd>${Math.round(a.avg_similarity * 100)}%</dd></div>
        </dl>
        <div class="alert-actions">
          <button class="btn small" type="button" data-ask="${esc(`Explain the pattern "${a.pattern_label}" across ${a.ncr_ids.join(", ")}. What worked before, and what should we do now?`)}">Ask about this pattern</button>
          <button class="btn small" type="button" data-ask="${esc(`Draft a SCAR for the most recent open NCR in ${a.ncr_ids.join(", ")}, using any earlier effective corrective action as the expected fix.`)}">Draft a SCAR</button>
        </div>
      </div>
    </article>`).join("");
  return data;
}

async function runDetector() {
  const btn = $("#run-detector");
  const before = (await api("alerts").catch(() => ({}))).updatedAt || null;
  btn.disabled = true;
  btn.textContent = "Detecting…";
  try {
    await api("alerts/run", { method: "POST" });
    const deadline = Date.now() + 4 * 60_000;
    while (Date.now() < deadline) {
      await sleep(5000);
      if (!$("#run-detector")) return;  // navigated away
      const data = await api("alerts");
      if (data.updatedAt && data.updatedAt !== before) {
        await drawAlerts();
        toast(`Detector finished: ${data.alerts.length} pattern${data.alerts.length === 1 ? "" : "s"}`);
        break;
      }
    }
  } catch (err) {
    toast(err.message);
  }
  const b = $("#run-detector");
  if (b) { b.disabled = false; b.textContent = "Run detector now"; }
}

// ------------------------------------------------------------------ Upload
const TRACK = [
  ["Uploaded", null],
  ["Reading", "ExtractText"],
  ["Structuring", "StructureRecord"],
  ["Saving", "PersistRecord"],
  ["Indexing", "SyncKnowledgeBase"],
];

function saveUploads() {
  sessionStorage.setItem("sqc.uploads", JSON.stringify(state.uploads.slice(0, 10)));
}

function uploadCard(u) {
  const idx = u.status === "SUCCEEDED" ? TRACK.length
    : Math.max(0, TRACK.findIndex(([, step]) => step === u.step));
  const failed = u.status && !["RUNNING", "SUCCEEDED", "QUEUED", "UPLOADING"].includes(u.status);
  const items = TRACK.map(([label], i) => {
    let cls = i < idx ? "done" : i === idx ? "now" : "";
    if (failed && i === idx) cls = "fail";
    return `<li class="${cls}">${label}</li>`;
  }).join("");
  const done = u.status === "SUCCEEDED";
  return `<div class="upload">
    <div class="upload-head"><b>${esc(u.fileName)}</b>
      ${done ? `<span class="tag accept">Ingested</span>` : failed ? `<span class="tag reject">Failed</span>` : `<span class="tag hold">In progress</span>`}</div>
    <ol class="track">${items}</ol>
    ${failed ? `<p class="meta-line">${esc(u.error || u.status)}</p>` : ""}
    ${done ? `<div class="alert-actions">
      ${u.ncrId ? ncrChip(u.ncrId) : ""}
      <a class="btn small" href="#/alerts">Check for patterns</a>
      ${u.ncrId ? `<button class="btn small" type="button" data-ask="${esc(`What do we know about ${u.ncrId}, and has this happened at other suppliers?`)}">Ask about it</button>` : ""}
    </div>` : ""}
  </div>`;
}

function drawUploads() {
  const box = $("#uploads");
  if (box) box.innerHTML = state.uploads.map(uploadCard).join("");
}

function renderUpload() {
  $("#view").innerHTML = `
    <div class="view-head"><div><h1>Upload an NCR</h1>
      <p>Drop an NCR, 8D or SCAR. Claude extracts a structured record, the document is indexed for search, and the pattern detector can compare it with every other supplier.</p></div></div>
    <label class="dropzone" id="dropzone">
      <h2>Drop a document here</h2>
      <p>Text, Markdown, single-page PDF, PNG or JPEG</p>
      <span class="btn">Choose a file</span>
      <input type="file" id="file" accept=".txt,.md,.pdf,.png,.jpg,.jpeg" hidden>
    </label>
    <p class="meta-line" style="margin-top:10px">For the demo, upload <code>data/demo/NCR-2026-0039.txt</code> from the repo.</p>
    <section class="uploads" id="uploads" aria-live="polite"></section>`;
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
  $("#file").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ""; });
  drawUploads();
  state.uploads.filter((u) => ["RUNNING", "QUEUED"].includes(u.status)).forEach(pollUpload);
}

async function uploadFile(file) {
  const u = { fileName: file.name, status: "UPLOADING", step: null };
  state.uploads.unshift(u);
  drawUploads();
  try {
    const target = await api("uploads", { method: "POST", body: { fileName: file.name } });
    const put = await fetch(target.url, { method: "PUT", headers: { "Content-Type": target.contentType }, body: file });
    if (!put.ok) throw new Error(`Upload to storage failed (${put.status})`);
    Object.assign(u, { key: target.key, status: "QUEUED", step: "ExtractText" });
    saveUploads();
    drawUploads();
    pollUpload(u);
  } catch (err) {
    Object.assign(u, { status: "FAILED", error: err.message });
    saveUploads();
    drawUploads();
  }
}

function pollUpload(u) {
  const tick = async () => {
    try {
      const s = await api(`uploads/status?key=${encodeURIComponent(u.key)}`);
      Object.assign(u, {
        status: s.status, step: s.step || (s.found ? u.step : "ExtractText"),
        ncrId: s.ncrId, error: s.error,
      });
      saveUploads();
      drawUploads();
      if (["RUNNING", "QUEUED"].includes(s.status)) {
        state.timers.add(setTimeout(tick, 3000));
      } else if (s.status === "SUCCEEDED") {
        toast(`${s.ncrId || u.fileName} ingested`);
      }
    } catch (err) {
      if (err.message !== "signed out") state.timers.add(setTimeout(tick, 5000));
    }
  };
  tick();
}

// ------------------------------------------------------------------ SCAR review
async function renderScars() {
  $("#view").innerHTML = `
    <div class="view-head"><div><h1>SCAR review</h1>
      <p>Drafts the copilot prepared. Nothing reaches a supplier from here: approving records the engineering decision for your SCAR process.</p></div></div>
    <section class="review"><ul class="scar-list" id="scar-list"><li class="meta-line">Loading drafts…</li></ul>
      <div id="scar-doc"></div></section>`;
  let data;
  try { data = await api("scars"); } catch (err) {
    $("#scar-list").innerHTML = `<li class="empty">${esc(err.message)}</li>`;
    return;
  }
  state.user.isReviewer = data.isReviewer;
  setCount("#scar-count", data.scars.filter((s) => s.status === "PENDING_REVIEW").length);
  if (!data.scars.length) {
    $("#scar-list").innerHTML = "";
    $("#scar-doc").innerHTML = `<div class="empty"><strong>No SCAR drafts yet</strong>
      Ask the copilot to draft one, for example from a cross-supplier pattern.
      <p style="margin-top:12px"><button class="btn small" type="button" data-ask="Draft a SCAR to Ardent Interconnect for their open gold plating NCRs, using any earlier effective corrective action as the expected fix.">Draft a SCAR for Ardent Interconnect</button></p></div>`;
    return;
  }
  $("#scar-list").innerHTML = data.scars.map((s) => `
    <li><button type="button" data-scar="${esc(s.key)}" aria-current="${s.key === state.selectedScar}">
      <span class="t">${esc(s.title || "SCAR draft")}</span>
      <span class="meta-line">${esc(s.supplier_id)}, drafted ${fmtDate(s.drafted_at)}</span>
      <span>${statusTag(s.status)}</span>
    </button></li>`).join("");
  const pick = data.scars.find((s) => s.key === state.selectedScar) || data.scars[0];
  openScar(pick.key);
}

async function openScar(key, editing = false) {
  state.selectedScar = key;
  document.querySelectorAll("[data-scar]").forEach((b) => b.setAttribute("aria-current", String(b.dataset.scar === key)));
  const box = $("#scar-doc");
  box.innerHTML = `<div class="scar-doc"><p class="meta-line">Loading…</p></div>`;
  let s;
  try { s = await api(`scars/item?key=${encodeURIComponent(key)}`); } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  const m = s.meta;
  const pending = m.status === "PENDING_REVIEW";
  const reviewed = m.reviewed_by
    ? `<p class="meta-line">${m.status === "APPROVED" ? "Approved" : "Rejected"} by ${esc(m.reviewed_by)} on ${fmtDate(m.reviewed_at)}${m.review_comment ? `: “${esc(m.review_comment)}”` : ""}</p>` : "";
  box.innerHTML = `<article class="scar-doc">
    <header>
      <div class="chips" style="margin-bottom:10px">${statusTag(m.status)}</div>
      <h2>${esc(m.title || "SCAR draft")}</h2>
      <p class="meta-line">To ${esc(m.supplier_id)}. ${esc(m.drafted_by || "")}, ${fmtDate(m.drafted_at)}
        ${m.last_edited_by ? `. Edited by ${esc(m.last_edited_by)} on ${fmtDate(m.last_edited_at)}` : ""}</p>
      ${reviewed}
      <div class="chips">${(m.ncr_ids || []).map(ncrChip).join("")}</div>
    </header>
    ${editing
      ? `<textarea id="scar-text" aria-label="SCAR markdown">${esc(s.markdown)}</textarea>
         <div class="decision"><div class="row">
           <button class="btn primary" type="button" id="scar-save">Save changes</button>
           <button class="btn" type="button" id="scar-cancel">Cancel</button></div></div>`
      : `<div class="doc">${md(s.markdown)}</div>
         ${pending ? `<div class="decision">
           ${s.isReviewer
             ? `<label for="scar-comment">Review comment (optional)</label>
                <textarea id="scar-comment" rows="2" placeholder="What should the supplier or the SCAR coordinator know?"></textarea>
                <div class="row">
                  <button class="btn approve" type="button" data-decide="approve">Approve SCAR</button>
                  <button class="btn reject" type="button" data-decide="reject">Reject draft</button>
                  <button class="btn" type="button" id="scar-edit">Edit draft</button></div>`
             : `<div class="notice">Only members of the reviewers group can approve or reject. You can still edit the draft.</div>
                <div class="row"><button class="btn" type="button" id="scar-edit">Edit draft</button></div>`}
         </div>` : ""}`}
  </article>`;

  $("#scar-edit")?.addEventListener("click", () => openScar(key, true));
  $("#scar-cancel")?.addEventListener("click", () => openScar(key));
  $("#scar-save")?.addEventListener("click", async () => {
    try {
      await api("scars/item", { method: "PUT", body: { key, markdown: $("#scar-text").value } });
      toast("Changes saved");
      openScar(key);
    } catch (err) { toast(err.message); }
  });
  box.querySelectorAll("[data-decide]").forEach((b) => b.addEventListener("click", async () => {
    const decision = b.dataset.decide;
    b.disabled = true;
    try {
      await api("scars/decision", { method: "POST", body: { key, decision, comment: $("#scar-comment")?.value || "" } });
      toast(decision === "approve" ? "SCAR approved" : "Draft rejected");
      renderScars();
    } catch (err) { toast(err.message); b.disabled = false; }
  }));
}

// ------------------------------------------------------------------ global events
document.addEventListener("click", (e) => {
  const ncr = e.target.closest("[data-ncr]");
  if (ncr) { openNcr(ncr.dataset.ncr); return; }
  const starter = e.target.closest("[data-starter]");
  if (starter) { ask(starter.dataset.starter); return; }
  const askBtn = e.target.closest("[data-ask]");
  if (askBtn) { askAbout(askBtn.dataset.ask); return; }
  const askNcr = e.target.closest("[data-ask-ncr]");
  if (askNcr) { askAbout(`What do we know about ${askNcr.dataset.askNcr}, and has this happened at other suppliers?`); return; }
  const scar = e.target.closest("[data-scar]");
  if (scar) { openScar(scar.dataset.scar); return; }
  if (e.target.id === "drawer") closeDrawer();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ------------------------------------------------------------------ boot
async function boot() {
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#sign-in").addEventListener("click", () => signIn());
  $("#sign-out").addEventListener("click", signOut);
  try {
    state.config = await (await fetch("config.json", { cache: "no-store" })).json();
  } catch {
    showGate("This app isn't configured yet. Deploy the SqcApp stack to publish config.json.");
    $("#sign-in").hidden = true;
    return;
  }
  try {
    await completeSignIn();
  } catch (err) {
    showGate(err.message);
    return;
  }
  if (!(await idToken())) { showGate(); return; }

  $("#gate").hidden = true;
  $("#shell").hidden = false;
  try {
    state.user = await api("me");
    $("#who").textContent = state.user.email + (state.user.isReviewer ? " (reviewer)" : "");
  } catch { /* shown on first API error */ }
  window.addEventListener("hashchange", route);
  if (!location.hash) location.hash = "#/ask";
  route();
  refreshCounts();
}

boot();
