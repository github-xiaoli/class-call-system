/* 班级叫号器 - 运行日志查看页（实时轮询 + 导出） */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { after: -1, paused: false, follow: true, seqs: new Set(), level: "all", q: "" };

async function fetchLogs() {
  const params = new URLSearchParams({ after: String(state.after), limit: "400" });
  if (state.level && state.level !== "all") params.set("level", state.level);
  if (state.q) params.set("q", state.q);
  let res = await fetch("/api/logs?" + params.toString());
  if (res.status === 401 && window.Auth) {
    const tok = await Auth.ensureToken();
    res = await fetch("/api/logs?" + params.toString(), { headers: { "X-Auth-Token": tok } });
  }
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function setStatus(text, kind) {
  const dot = $("dot");
  dot.className = "dot" + (kind ? " " + kind : "");
  $("status").innerHTML = "";
  $("status").appendChild(dot);
  $("status").appendChild(document.createTextNode(" " + text));
}

function highlight(text, kw) {
  const span = document.createElement("span");
  if (!kw) {
    span.textContent = text;
    return span;
  }
  const low = text.toLowerCase();
  const k = kw.toLowerCase();
  let i = 0;
  let at = low.indexOf(k);
  while (at >= 0) {
    if (at > i) span.appendChild(document.createTextNode(text.slice(i, at)));
    const m = document.createElement("mark");
    m.textContent = text.slice(at, at + k.length);
    span.appendChild(m);
    i = at + k.length;
    at = low.indexOf(k, i);
  }
  if (i < text.length) span.appendChild(document.createTextNode(text.slice(i)));
  return span;
}

function appendLines(lines) {
  const view = $("view");
  const empty = $("empty");
  if (empty) empty.remove();
  const frag = document.createDocumentFragment();
  for (const ln of lines) {
    if (state.seqs.has(ln.seq)) continue;
    state.seqs.add(ln.seq);
    const row = document.createElement("div");
    row.className = "ln " + (ln.level || "info");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = ln.t;
    const x = document.createElement("span");
    x.className = "x";
    x.appendChild(highlight(ln.text, state.q));
    row.appendChild(t);
    row.appendChild(x);
    frag.appendChild(row);
  }
  view.appendChild(frag);
  // 只保留最近 2000 行，避免页面越开越卡
  while (view.childElementCount > 2000) view.removeChild(view.firstElementChild);
  if (state.follow) view.scrollTop = view.scrollHeight;
}

async function tick() {
  try {
    const data = await fetchLogs();
    setStatus("实时日志 · 已显示 " + state.seqs.size + " 行", "live");
    if (data.file) $("file").textContent = data.file;
    const lines = data.lines || [];
    if (lines.length) appendLines(lines);
    if (typeof data.next === "number" && data.next > state.after) state.after = data.next;
  } catch (e) {
    setStatus("读取失败：" + e.message + "（稍后自动重试）", "dead");
  }
}

let timer = null;
function startPolling() {
  if (timer) clearInterval(timer);
  tick();
  timer = setInterval(tick, 1000);
}
function stopPolling() {
  if (timer) clearInterval(timer);
  timer = null;
}

async function exportLogs() {
  try {
    let res = await fetch("/api/logs/export");
    if (res.status === 401 && window.Auth) {
      const tok = await Auth.ensureToken();
      res = await fetch("/api/logs/export", { headers: { "X-Auth-Token": tok } });
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    a.href = url;
    a.download = "班级叫号器-日志-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) +
      "-" + pad(d.getHours()) + pad(d.getMinutes()) + ".txt";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    alert("导出失败：" + e.message);
  }
}

function bind() {
  $("btn-pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("btn-pause").textContent = state.paused ? "▶️ 继续" : "⏸ 暂停";
    if (state.paused) stopPolling();
    else startPolling();
  });
  $("btn-clear").addEventListener("click", () => {
    $("view").innerHTML = "";
    state.seqs.clear();
  });
  $("btn-export").addEventListener("click", exportLogs);
  $("btn-home").addEventListener("click", () => { location.href = "/"; });
  $("search").addEventListener("input", () => {
    state.q = $("search").value.trim();
    state.after = -1;
    state.seqs.clear();
    $("view").innerHTML = "";
    tick();
  });
  $("level").addEventListener("change", () => {
    state.level = $("level").value;
    state.after = -1;
    state.seqs.clear();
    $("view").innerHTML = "";
    tick();
  });
  $("follow").addEventListener("change", () => { state.follow = $("follow").checked; });
  // 页面被切到后台时省电：暂停轮询，回来继续
  document.addEventListener("visibilitychange", () => {
    if (state.paused) return;
    if (document.hidden) stopPolling();
    else startPolling();
  });
}

bind();
startPolling();
