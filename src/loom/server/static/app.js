/* Loom desktop — a Claude Code-style client over the Loom agent server.
   Plain JS on purpose: no build step, `pip install` is the whole toolchain. */

"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  view: "home",
  me: { display_name: "", model: "" },
  session: null,        // meta of the open session
  ws: null,
  wsSession: null,      // session id the socket belongs to
  busy: false,
  dir: "",              // picked folder for the NEXT session
  modelOverride: "",    // model for the NEXT session
  skills: [],
  pendingFirstMessage: null,
  slash: { open: false, items: [], sel: 0 },
  compactAt: 150000,    // context ring "full" point (server: /api/state)
};

const BUILTIN_COMMANDS = [
  { name: "cost", desc: "Spend + frontier-baseline savings this session" },
  { name: "rewind", desc: "Undo the file changes of the last turn" },
  { name: "model", desc: "Switch the workhorse model — /model <litellm-id>" },
  { name: "clear", desc: "Start a fresh conversation (files stay)" },
];

const MODEL_PRESETS = [
  "openrouter/moonshotai/kimi-k2",
  "openrouter/z-ai/glm-5.1",
  "openrouter/deepseek/deepseek-v4",
  "anthropic/claude-sonnet-5",
  "openai/gpt-5.2",
];

/* ── plumbing ──────────────────────────────────────────────────────────────── */

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* Small markdown renderer: code blocks, headers, bold/italic/inline code, lists, links. */
function md(text) {
  const blocks = [];
  let t = String(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code)}</code></pre>`);
    return `\x00${blocks.length - 1}\x00`;
  });
  t = esc(t);
  t = t.replace(/^### (.*)$/gm, "<h3>$1</h3>")
       .replace(/^## (.*)$/gm, "<h2>$1</h2>")
       .replace(/^# (.*)$/gm, "<h1>$1</h1>")
       .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
       .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<i>$2</i>")
       .replace(/`([^`\n]+)`/g, "<code>$1</code>")
       .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  // lists
  t = t.replace(/((?:^[-*] .*\n?)+)/gm, (m) =>
    "<ul>" + m.trim().split(/\n/).map((l) => `<li>${l.replace(/^[-*] /, "")}</li>`).join("") + "</ul>");
  t = t.replace(/((?:^\d+\. .*\n?)+)/gm, (m) =>
    "<ol>" + m.trim().split(/\n/).map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`).join("") + "</ol>");
  // paragraphs
  t = t.split(/\n{2,}/).map((p) =>
    /^<(h\d|ul|ol|pre)/.test(p.trim()) ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
  return t.replace(/\x00(\d+)\x00/g, (_, i) => blocks[+i]);
}

function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function fmtHour(h) {
  if (h === null || h === undefined) return "—";
  const ampm = h < 12 ? "AM" : "PM";
  return `${h % 12 === 0 ? 12 : h % 12} ${ampm}`;
}

function modelTail(m) { return m ? m.split("/").pop() : "—"; }

/* ── views ─────────────────────────────────────────────────────────────────── */

const VIEWS = ["home", "session", "artifacts", "settings"];

function show(view) {
  state.view = view;
  for (const v of VIEWS) $(`#view-${v}`).classList.toggle("hidden", v !== view);
  $("#composer").classList.toggle("hidden", view !== "home" && view !== "session");
  $("#ctx-ring").classList.toggle("hidden", view !== "session");
  $("#queue-badge").classList.add("hidden");
  if (view !== "session") setStatus("");
  renderRecents();
}

/* ── context ring: this session's token usage, filling toward auto-compaction ── */

function updateRing(tokens) {
  if (tokens === undefined || tokens === null) return;
  const frac = Math.min(1, tokens / state.compactAt);
  const C = 2 * Math.PI * 9; // circle r=9
  const fill = $("#ring-fill");
  fill.style.strokeDasharray = C.toFixed(2);
  fill.style.strokeDashoffset = (C * (1 - frac)).toFixed(2);
  fill.style.stroke = frac > 0.9 ? "var(--red, #e5534b)"
    : frac > 0.7 ? "var(--orange, #d29922)" : "var(--accent)";
  $("#ctx-tokens").textContent = fmtTokens(tokens);
  $("#ctx-ring").title =
    `Context: ~${tokens.toLocaleString()} tokens (${Math.round(frac * 100)}% of the ` +
    `${fmtTokens(state.compactAt)} auto-compaction point)`;
}

/* ── task ledger panel ────────────────────────────────────────────────────────── */

async function loadTasks() {
  if (!state.session) return;
  let items = [];
  try { items = await api(`/api/sessions/${state.session.id}/tasks`); } catch (e) { return; }
  const panel = $("#tasks-panel");
  panel.classList.toggle("hidden", items.length === 0);
  if (!items.length) return;
  const mark = { pending: "○", in_progress: "◐", completed: "●" };
  const open = items.filter((t) => t.status !== "completed").length;
  $("#tasks-count").textContent = open ? `${open} open` : "all done";
  $("#tasks-list").innerHTML = items.map((t) =>
    `<div class="task-row ${esc(t.status)}">
       <span class="task-mark">${mark[t.status] || "○"}</span>
       <span class="task-subject">${esc(t.subject)}</span>
     </div>`).join("");
}

async function showHome() {
  show("home");
  state.session = null;
  closeWs();
  updateChips();
  try {
    const stats = await api("/api/stats");
    renderStats(stats);
  } catch (e) { /* stats are cosmetic */ }
}

function renderStats(s) {
  const cells = [
    ["Sessions", s.sessions],
    ["Messages", s.messages.toLocaleString()],
    ["Total tokens", fmtTokens(s.tokens)],
    ["Active days", s.active_days],
    ["Current streak", s.current_streak + "d"],
    ["Longest streak", s.longest_streak + "d"],
    ["Peak hour", fmtHour(s.peak_hour)],
    ["Favorite model", modelTail(s.favorite_model)],
  ];
  $("#stat-grid").innerHTML = cells.map(([l, v]) =>
    `<div class="stat"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`).join("");
  $("#heatmap").innerHTML = s.heatmap.map((c) => {
    const lvl = c.count === 0 ? "" : c.count === 1 ? "l1" : c.count <= 3 ? "l2" : c.count <= 6 ? "l3" : "l4";
    return `<span class="hm-cell ${lvl}" title="${c.date}: ${c.count}"></span>`;
  }).join("");
  $("#fun-line").textContent = s.fun_line;
}

async function showArtifacts() {
  show("artifacts");
  const items = await api("/api/artifacts");
  $("#artifact-list").innerHTML = items.length
    ? items.map((a, i) =>
        `<div class="artifact" data-i="${i}">
           <span class="path">${esc(a.path)}</span>
           <span class="meta">${esc(a.action)} · ${esc(a.session_title)} · ${new Date(a.ts * 1000).toLocaleString()}</span>
         </div>`).join("")
    : '<p class="dim">Nothing yet — files the agent writes will show up here.</p>';
  document.querySelectorAll(".artifact").forEach((el) =>
    el.addEventListener("click", () => previewFile(items[+el.dataset.i])));
}

async function previewFile(a) {
  try {
    const f = await api(`/api/file?workdir=${encodeURIComponent(a.workdir)}&path=${encodeURIComponent(a.path)}`);
    $("#file-title").textContent = a.path + (f.truncated ? " (truncated)" : "");
    $("#file-body").textContent = f.content;
    $("#modal-file").classList.remove("hidden");
  } catch (e) {
    alert("Cannot preview: " + e.message);
  }
}

async function showSettings() {
  show("settings");
  const s = await api("/api/settings");
  const form = $("#settings-form");
  const row = (k, v, secret) =>
    `<label for="set-${k}">${k}</label>
     <input id="set-${k}" name="${k}" value="${esc(v || "")}" ${secret ? 'autocomplete="off"' : ""}
            placeholder="${secret ? "paste key…" : ""}">`;
  form.innerHTML =
    '<div class="settings-head">API keys (any one is enough)</div>' +
    s.provider_keys.map((k) => row(k, s.values[k], true)).join("") +
    '<div class="settings-head">Agent</div>' +
    s.plain_fields.map((k) => row(k, s.values[k], false)).join("");
}

async function saveSettings() {
  const updates = {};
  document.querySelectorAll("#settings-form input").forEach((el) => { updates[el.name] = el.value; });
  await api("/api/settings", { method: "POST", body: updates });
  $("#settings-saved").textContent = "Saved ✓";
  setTimeout(() => { $("#settings-saved").textContent = ""; }, 2500);
  await loadState();
}

/* ── sessions & recents ────────────────────────────────────────────────────── */

async function renderRecents() {
  let metas = [];
  try { metas = await api("/api/sessions"); } catch (e) { return; }
  $("#recents").innerHTML = metas.map((m) =>
    `<div class="recent ${state.session && state.session.id === m.id ? "active" : ""}" data-id="${m.id}">
       <span class="dot"></span><span class="title">${esc(m.title)}</span>
       <button class="del" title="Delete session">✕</button>
     </div>`).join("");
  document.querySelectorAll(".recent").forEach((el) => {
    el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("del")) return;
      openSession(el.dataset.id);
    });
    el.querySelector(".del").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await api(`/api/sessions/${el.dataset.id}`, { method: "DELETE" });
      if (state.session && state.session.id === el.dataset.id) showHome();
      else renderRecents();
    });
  });
}

async function openSession(id) {
  const data = await api(`/api/sessions/${id}`);
  state.session = data.meta;
  show("session");
  $("#session-title").textContent = data.meta.title;
  $("#session-sub").textContent = `${data.meta.workdir} · ${modelTail(data.meta.model)}`;
  updateCost(data.meta.total_cost, data.meta.baseline_cost);
  updateRing(data.meta.context_tokens);
  loadTasks();
  updateChips();
  const t = $("#transcript");
  t.innerHTML = "";
  for (const ev of data.events) renderEvent(ev, false);
  t.scrollTop = t.scrollHeight;
  connectWs(id);
  await loadSkills(data.meta.workdir);
  renderRecents();
}

async function newSession(firstMessage) {
  if (!state.dir) {
    state.pendingFirstMessage = firstMessage || null;
    openDirPicker();
    return;
  }
  const meta = await api("/api/sessions", {
    method: "POST",
    body: { workdir: state.dir, model: state.modelOverride || undefined },
  });
  await openSession(meta.id);
  if (firstMessage) sendToSession(firstMessage);
}

function updateCost(total, baseline) {
  const saved = baseline - total;
  const pct = baseline > 0 ? Math.round((saved / baseline) * 100) : 0;
  $("#session-cost").innerHTML =
    `$${total.toFixed(4)} spent<br><span style="color:var(--green)">saved $${saved.toFixed(4)} (${pct}%)</span>`;
}

function updateChips() {
  const dir = state.session ? state.session.workdir : state.dir;
  $("#chip-dir-name").textContent = dir ? dir.split(/[\\/]/).pop() : "pick a folder";
  const model = state.session ? state.session.model : (state.modelOverride || state.me.model);
  $("#chip-model-name").textContent = modelTail(model);
}

/* ── websocket ─────────────────────────────────────────────────────────────── */

function closeWs() {
  if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; state.wsSession = null; }
}

function connectWs(sessionId) {
  closeWs();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  state.ws = ws;
  state.wsSession = sessionId;
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = () => {
    if (state.session && state.session.id === sessionId && state.view === "session") {
      setTimeout(() => { if (state.wsSession === sessionId || !state.ws) connectWs(sessionId); }, 1500);
    }
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "hello":
      state.busy = !!ev.busy;
      setStatus(state.busy ? "working…" : "");
      return;
    case "busy":
      setStatus("the agent is still working — hold on");
      return;
    case "turn_end":
      state.busy = false;
      setStatus("");
      updateCost(ev.total_cost, ev.baseline_cost);
      updateRing(ev.context_tokens);
      updateQueueBadge(ev.queued || 0);
      if (ev.activity && ev.activity !== "No tool activity")
        renderEvent({ type: "activity", text: ev.activity }, true);
      loadTasks();
      renderRecents();
      return;
    case "queued":
      updateQueueBadge((state.queued || 0) + 1);
      break;
    case "user":
      state.busy = true;
      setStatus("✳ thinking…");
      break;
    case "step":
      setStatus(`step ${ev.step} · ${modelTail(ev.model)} · $${ev.cost.toFixed(4)}`);
      break;
  }
  renderEvent(ev, true);
}

function updateQueueBadge(n) {
  state.queued = n;
  const b = $("#queue-badge");
  b.classList.toggle("hidden", !n);
  b.textContent = n ? `⏳ ${n} queued` : "";
}

function setStatus(text) {
  $("#status-line").innerHTML = text
    ? (state.busy ? '<span class="spin">✳</span> ' : "") + esc(text)
    : "";
}

/* ── transcript rendering ──────────────────────────────────────────────────── */

function el(html) {
  const div = document.createElement("div");
  div.innerHTML = html;
  return div.firstElementChild;
}

function append(node) {
  const t = $("#transcript");
  const stick = t.scrollHeight - t.scrollTop - t.clientHeight < 160;
  t.appendChild(node);
  if (stick) t.scrollTop = t.scrollHeight;
}

function diffHtml(text) {
  return String(text).split("\n").map((l) => {
    const cls = l.startsWith("+") ? "diff-add" : l.startsWith("-") ? "diff-del"
      : l.startsWith("@@") ? "diff-hunk" : "";
    return `<span class="diff-line ${cls}">${esc(l)}</span>`;
  }).join("");
}

function renderEvent(ev, live) {
  const sub = ev.subagent ? " sub" : "";
  switch (ev.type) {
    case "user":
      append(el(`<div class="msg-user">${esc(ev.text)}</div>`));
      break;
    case "assistant_text":
      append(el(`<div class="msg-assistant${sub ? ' dim' : ''}" ${sub ? 'style="margin-left:26px"' : ""}>${md(ev.text)}</div>`));
      break;
    case "step": {
      const escTag = ev.escalated ? ' <span class="esc">↑ escalated</span>' : "";
      append(el(`<div class="evt-step"${sub ? ' style="margin-left:26px"' : ""}>step ${ev.step} · ${esc(modelTail(ev.model))}${escTag} · ${Number(ev.tokens).toLocaleString()} tok · $${Number(ev.cost).toFixed(4)}</div>`));
      break;
    }
    case "tool": {
      let argsText = ev.arguments || "";
      try { argsText = JSON.stringify(JSON.parse(ev.arguments || "{}")); } catch (e) { /* raw */ }
      const isDiffy = ev.name === "write_file" || ev.name === "edit_file";
      const body = `<b>args</b>\n${esc(argsText)}\n\n<b>result</b>\n${isDiffy ? esc(ev.result || "") : esc(ev.result || "")}`;
      const card = el(
        `<div class="tool-card${sub}">
           <div class="tool-head">
             <span class="name">⚙ ${esc(ev.name)}</span>
             <span class="args">${esc(argsText.slice(0, 120))}</span>
             <span class="outcome ${esc(ev.outcome)}">${esc(ev.outcome)}</span>
           </div>
           <div class="tool-body">${body}</div>
         </div>`);
      card.querySelector(".tool-head").addEventListener("click", () => card.classList.toggle("open"));
      append(card);
      break;
    }
    case "permission_request":
      if (live) renderPermission(ev);
      break;
    case "critic_start":
      append(el(`<div class="evt-note">🔍 critic (${esc(modelTail(ev.model))}) reviewing the diff…</div>`));
      break;
    case "critic_verdict":
      append(ev.approved
        ? el('<div class="evt-note critic-ok">critic · approved ✓</div>')
        : el(`<div class="evt-note critic-no">critic · revise: ${esc(ev.feedback || "")}</div>`));
      break;
    case "critic_error":
      append(el(`<div class="evt-note">critic unavailable (${esc(ev.error)}) — accepted without review</div>`));
      break;
    case "subagent_start":
      append(el(`<div class="evt-sub">⤷ ${esc(ev.agent_type)} subagent — ${esc(ev.description || "")}</div>`));
      break;
    case "subagent_done":
      append(el(`<div class="evt-sub">⤶ subagent done · ${ev.steps} step(s) · $${Number(ev.cost).toFixed(4)}</div>`));
      break;
    case "done":
      append(el(`<div class="evt-done">✓ done · turn $${Number(ev.turn_cost).toFixed(4)} · ${esc(ev.summary)}</div>`));
      break;
    case "model_error":
      append(el(`<div class="evt-note error">✗ model call failed: ${esc(ev.error)}${ev.key_hint ? " — check your API key in Customize" : ""}</div>`));
      break;
    case "budget_stop":
      append(el(`<div class="evt-note error">budget reached ($${Number(ev.total_cost).toFixed(4)}) — stopped</div>`));
      break;
    case "max_steps":
      append(el(`<div class="evt-note error">hit max steps (${ev.max_steps}) — stopped</div>`));
      break;
    case "command_result":
      append(el(`<div class="evt-note">${esc(ev.text)}</div>`));
      break;
    case "activity":
      append(el(`<div class="evt-activity">${esc(ev.text)}</div>`));
      break;
    case "compaction": {
      // live event: {trigger, pre_tokens, post_tokens}; stored: {meta: {trigger, preTokens…}}
      const m = ev.meta || {};
      const trigger = ev.trigger || m.trigger || "";
      const pre = ev.pre_tokens ?? m.preTokens;
      const post = ev.post_tokens ?? m.postTokens;
      const sizes = pre !== undefined ? ` · ${fmtTokens(pre)} → ${fmtTokens(post)} tok` : "";
      append(el(`<div class="evt-compaction">═══ context compacted (${esc(trigger)})${sizes} ═══</div>`));
      if (live && ev.post_tokens !== undefined) updateRing(ev.post_tokens);
      break;
    }
    case "goal_veto":
      append(el(`<div class="evt-note critic-no">stop hook · goal not met: ${esc(ev.feedback || "")}</div>`));
      break;
  }
}

function renderPermission(ev) {
  const card = el(
    `<div class="perm-card">
       <div class="perm-title">⚠ ${esc(ev.tool)} wants to run</div>
       <div class="perm-preview">${diffHtml(ev.preview)}</div>
       <div class="perm-actions">
         <button class="btn accent" data-a="y">Allow once</button>
         <button class="btn" data-a="a">Always this session</button>
         <button class="btn deny" data-a="n">Deny</button>
       </div>
       <div class="perm-note"></div>
     </div>`);
  card.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      if (state.ws) state.ws.send(JSON.stringify({ type: "permission_response", id: ev.id, answer: b.dataset.a }));
      card.querySelector(".perm-actions").remove();
      card.querySelector(".perm-note").textContent =
        b.dataset.a === "n" ? "Denied." : b.dataset.a === "a" ? "Allowed for the rest of the session." : "Allowed once.";
    }));
  append(card);
}

/* ── composer / slash menu ─────────────────────────────────────────────────── */

async function loadSkills(workdir) {
  try { state.skills = await api(`/api/skills?workdir=${encodeURIComponent(workdir || ".")}`); }
  catch (e) { state.skills = []; }
}

function slashItems(filter) {
  const all = [
    ...BUILTIN_COMMANDS.map((c) => ({ ...c, kind: "command" })),
    ...state.skills.map((s) => ({ name: s.name, desc: `${s.description} (${s.source} skill)`, kind: "skill" })),
  ];
  return all.filter((c) => c.name.startsWith(filter)).slice(0, 12);
}

function updateSlashMenu() {
  const v = $("#input").value;
  const m = /^\/(\w*)$/.exec(v.split("\n")[0]) || (v.startsWith("/") && !v.includes(" ") ? [v, v.slice(1)] : null);
  if (state.view === "session" && m) {
    state.slash.items = slashItems(m[1]);
    state.slash.open = state.slash.items.length > 0;
    state.slash.sel = 0;
  } else {
    state.slash.open = false;
  }
  const menu = $("#slash-menu");
  menu.classList.toggle("hidden", !state.slash.open);
  if (state.slash.open) {
    menu.innerHTML = state.slash.items.map((c, i) =>
      `<div class="slash-item ${i === state.slash.sel ? "sel" : ""}" data-i="${i}">
         <span class="cmd">/${esc(c.name)}</span><span class="desc">${esc(c.desc)}</span>
       </div>`).join("");
    menu.querySelectorAll(".slash-item").forEach((elm) =>
      elm.addEventListener("mousedown", (ev) => { ev.preventDefault(); pickSlash(+elm.dataset.i); }));
  }
}

function pickSlash(i) {
  const item = state.slash.items[i];
  if (!item) return;
  $("#input").value = `/${item.name} `;
  state.slash.open = false;
  $("#slash-menu").classList.add("hidden");
  $("#input").focus();
}

function sendToSession(text) {
  if (!state.ws || state.ws.readyState !== 1) return;
  if (text.startsWith("/")) {
    const [cmd, ...rest] = text.slice(1).split(" ");
    const arg = rest.join(" ").trim();
    if (BUILTIN_COMMANDS.some((c) => c.name === cmd)) {
      state.ws.send(JSON.stringify({ type: "command", name: cmd, arg }));
    } else {
      state.ws.send(JSON.stringify({ type: "skill", name: cmd, arg }));
    }
    return;
  }
  state.ws.send(JSON.stringify({ type: "user_message", text }));
}

async function onSend() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "auto";
  updateSlashMenu();
  if (state.view === "session" && state.session) sendToSession(text);
  else await newSession(text);
}

/* ── directory picker ──────────────────────────────────────────────────────── */

let dirCurrent = "";

async function browse(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
  dirCurrent = data.path;
  $("#dir-path").textContent = data.path;
  $("#dir-up").disabled = !data.parent;
  $("#dir-up").dataset.parent = data.parent || "";
  $("#dir-list").innerHTML = data.dirs.map((d) => `<div class="dir-entry">📁 ${esc(d)}</div>`).join("")
    || '<div class="dir-entry dim">(no subfolders)</div>';
  document.querySelectorAll("#dir-list .dir-entry").forEach((elm, i) => {
    if (data.dirs[i]) elm.addEventListener("click", () => browse(data.path.replace(/[\\/]$/, "") + "/" + data.dirs[i]));
  });
}

function openDirPicker() {
  $("#modal-dir").classList.remove("hidden");
  browse(state.dir || "");
}

/* ── boot ──────────────────────────────────────────────────────────────────── */

async function loadState() {
  const s = await api("/api/state");
  state.me = s;
  if (s.compact_at) state.compactAt = s.compact_at;
  const name = s.display_name || "";
  $("#greeting-text").textContent = name ? `What’s up next, ${name}?` : "What’s up next?";
  $("#me-name").textContent = name || "Loom";
  $("#me-avatar").textContent = (name || "L").slice(0, 1).toUpperCase();
  $("#key-banner").classList.toggle("hidden", s.has_key);
  updateChips();
}

function bind() {
  $("#btn-new").addEventListener("click", () => { state.dir = ""; state.pendingFirstMessage = null; openDirPicker(); });
  $("#btn-artifacts").addEventListener("click", showArtifacts);
  $("#btn-settings").addEventListener("click", showSettings);
  $("#btn-save-settings").addEventListener("click", (e) => { e.preventDefault(); saveSettings(); });
  $("#key-banner-link").addEventListener("click", (e) => { e.preventDefault(); showSettings(); });
  $("#btn-send").addEventListener("click", onSend);
  $("#chip-dir").addEventListener("click", () => { if (!state.session) openDirPicker(); });
  $("#chip-model").addEventListener("click", onModelChip);
  $("#dir-cancel").addEventListener("click", () => $("#modal-dir").classList.add("hidden"));
  $("#dir-up").addEventListener("click", (e) => { const p = e.currentTarget.dataset.parent; if (p) browse(p); });
  $("#dir-use").addEventListener("click", async () => {
    state.dir = dirCurrent;
    $("#modal-dir").classList.add("hidden");
    updateChips();
    const pending = state.pendingFirstMessage;
    state.pendingFirstMessage = null;
    if (pending !== null) await newSession(pending || undefined);
    else await newSession();
  });
  $("#file-close").addEventListener("click", () => $("#modal-file").classList.add("hidden"));

  const input = $("#input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
    updateSlashMenu();
  });
  input.addEventListener("keydown", (e) => {
    if (state.slash.open) {
      if (e.key === "ArrowDown") { e.preventDefault(); state.slash.sel = (state.slash.sel + 1) % state.slash.items.length; updateSlashMenu(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); state.slash.sel = (state.slash.sel - 1 + state.slash.items.length) % state.slash.items.length; updateSlashMenu(); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); pickSlash(state.slash.sel); return; }
      if (e.key === "Escape") { state.slash.open = false; $("#slash-menu").classList.add("hidden"); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); }
  });
}

function onModelChip() {
  const preset = MODEL_PRESETS.map((m, i) => `${i + 1}. ${m}`).join("\n");
  const answer = prompt(
    `Model (LiteLLM id) — type one, or a number for a preset:\n${preset}`,
    state.session ? state.session.model : (state.modelOverride || state.me.model));
  if (!answer) return;
  const chosen = /^\d+$/.test(answer.trim()) ? MODEL_PRESETS[+answer.trim() - 1] : answer.trim();
  if (!chosen) return;
  if (state.session && state.ws) {
    state.ws.send(JSON.stringify({ type: "command", name: "model", arg: chosen }));
    state.session.model = chosen;
  } else {
    state.modelOverride = chosen;
  }
  updateChips();
}

(async function boot() {
  bind();
  await loadState();
  await showHome();
})();
