/* 班级叫号器 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  roster: [],
  calls: [],
  config: {},
  selected: { number: "", name: "" },
  numInput: "",
  teacher: "",
  csvFileId: null,
  csvColumns: [],
  interfaces: [],
  isLocal: false,
  localAdmin: false,
  user: null,
};

/* ---------- 工具 ---------- */
function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}
function setCookie(name, value, days = 365) {
  const d = new Date();
  d.setTime(d.getTime() + days * 24 * 3600 * 1000);
  document.cookie = name + "=" + encodeURIComponent(value) + "; expires=" + d.toUTCString() + "; path=/";
}
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}
async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) {
    const e = new Error(data.error || ("请求失败 " + res.status));
    e.status = res.status;
    e.data = data;
    throw e;
  }
  return data;
}
/* 带令牌请求：遇到 401 时自动挑战签名取令牌重试一次（本机访问不会 401） */
async function authedApi(url, options = {}) {
  let res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    const tok = await Auth.ensureToken();
    res = await fetch(url, {
      ...options,
      headers: { ...(options.headers || {}), "X-Auth-Token": tok, "Content-Type": "application/json" },
    });
  }
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) {
    const e = new Error(data.error || ("请求失败 " + res.status));
    e.status = res.status;
    e.data = data;
    throw e;
  }
  return data;
}
/* 带令牌的 FormData 上传（本机直接放行，远程 401 时自动取令牌重试） */
async function authedForm(url, fd) {
  let res = await fetch(url, { method: "POST", body: fd });
  if (res.status === 401) {
    const tok = await Auth.ensureToken();
    res = await fetch(url, { method: "POST", headers: { "X-Auth-Token": tok }, body: fd });
  }
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) {
    const e = new Error(data.error || ("请求失败 " + res.status));
    e.status = res.status;
    e.data = data;
    throw e;
  }
  return data;
}
function maybeActivate(e) {
  // 统一登录后：没有权限一律引导去登录页
  if (e && (e.code === "NEED_ACTIVATE" || e.code === "NEED_LOGIN"
      || /未激活|需要密码激活|请先登录/.test(e.message || ""))) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    return true;
  }
  // 叫号节流：给个轻提示并让按钮冷却，不要静默吞掉
  if (e && e.status === 429 && e.data && e.data.throttled) {
    toast(e.message);
    coolDownCallButton(e.data.retry_after || 2);
    return true;
  }
  return false;
}

/* 叫号按钮冷却：显示剩余秒数，期间不可点，避免用户狂点 */
function coolDownCallButton(seconds) {
  const btn = $("btn-call");
  if (!btn) return;
  const total = Math.max(0.5, Number(seconds) || 2);
  let left = total;
  const original = btn.dataset.label || btn.textContent;
  btn.dataset.label = original;
  btn.disabled = true;
  btn.textContent = "⏳ " + left.toFixed(1) + " 秒";
  const timer = setInterval(() => {
    left = Math.round((left - 0.1) * 10) / 10;
    if (left <= 0) {
      clearInterval(timer);
      btn.disabled = false;
      btn.textContent = btn.dataset.label;
    } else {
      btn.textContent = "⏳ " + left.toFixed(1) + " 秒";
    }
  }, 100);
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

/* ---------- 弹窗 ---------- */
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }
document.querySelectorAll(".modal-mask").forEach((mask) => {
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.classList.add("hidden"); });
});
document.querySelectorAll("[data-close]").forEach((b) => {
  b.addEventListener("click", () => b.closest(".modal-mask").classList.add("hidden"));
});

/* ---------- 加载数据 ---------- */
async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("call-count").textContent = s.call_count;
    $("roster-count").textContent = s.roster_count;
    const urls = [];
    for (const ip of s.local_ips) urls.push("http://" + ip + ":" + s.port);
    if (s.mdns_enabled) urls.push(s.mdns_url);
    if (s.secure_enabled) urls.push(s.secure_url + " (安全)");
    $("server-url").textContent = urls.join("  ·  ");
    state.secureUrl = s.secure_url;
    state.securePort = s.secure_port;
    $("status-dot").className = "dot " + (s.zeroconf_ok ? "ok" : "err");
  } catch (e) {
    $("status-dot").className = "dot err";
  }
}

async function loadAuthMe() {
  try {
    const d = await fetch("/api/auth/session").then((r) => r.json());
    state.user = d.user || null;
    state.isLocal = !!d.local;
    state.localAdmin = !!d.local_admin;
  } catch (e) { /* 忽略 */ }
  const admin = !!(state.user && state.user.is_admin);
  $("btn-admin").classList.toggle("hidden", !admin);
  const u = $("teacher-name");
  if (u && state.user && !u.value) {
    u.value = state.user.display_name || state.user.username || "";
  }
}

async function loadRoster() {
  try {
    const d = await api("/api/roster");
    state.roster = d.roster || [];
  } catch (e) {
    state.roster = [];
  }
  renderRoster();
}

async function loadConfig() {
  try {
    const d = await authedApi("/api/settings");
    state.config = d.config || {};
    state.interfaces = d.interfaces || [];
  } catch (e) { state.config = {}; state.interfaces = []; }
}

async function loadCalls() {
  try {
    const d = await api("/api/calls");
    state.calls = d.calls || [];
  } catch (e) {
    state.calls = [];
  }
  renderCalls();
}

function renderCalls() {
  const ul = $("call-list");
  ul.innerHTML = "";
  $("call-total").textContent = state.calls.length;
  $("call-list-empty").classList.toggle("hidden", state.calls.length > 0);
  [...state.calls].reverse().forEach((c) => {
    const li = document.createElement("li");
    li.innerHTML = '<span class="ct">' + esc((c.time || "").slice(11, 19)) + '</span>' +
      '<span class="cn">' + esc(c.number) + " " + esc(c.name) + '</span>' +
      '<span class="cp">' + esc(c.phrase || "") + '</span>';
    ul.appendChild(li);
  });
}

/* ---------- 名单渲染 ---------- */
function sortRoster(list) {
  return [...list].sort((a, b) =>
    String(a.number).localeCompare(String(b.number), "zh", { numeric: true }));
}

function renderRoster() {
  const kw = ($("search").value || "").trim().toLowerCase();
  const list = sortRoster(state.roster).filter((it) =>
    !kw || String(it.number).toLowerCase().includes(kw) || String(it.name).toLowerCase().includes(kw));
  const ul = $("student-list");
  ul.innerHTML = "";
  $("roster-count").textContent = state.roster.length;
  $("list-empty").classList.toggle("hidden", state.roster.length > 0);
  if (list.length === 0 && state.roster.length > 0) {
    $("list-empty").textContent = "没有匹配的学生";
    $("list-empty").classList.remove("hidden");
  } else {
    $("list-empty").textContent = "暂无学生，请导入 CSV 或手动添加";
  }
  for (const it of list) {
    const li = document.createElement("li");
    li.className = "stu-item";
    li.dataset.number = it.number;
    const active = state.selected.number === it.number;
    if (active) li.classList.add("active");
    li.innerHTML = '<span class="num">' + esc(it.number) + '</span><span class="nm">' + esc(it.name) + "</span>";
    li.addEventListener("click", () => selectStudent(it.number, it.name));
    ul.appendChild(li);
  }
}

function selectStudent(number, name) {
  state.selected = { number, name };
  state.numInput = String(number);
  $("num-display").textContent = state.numInput;
  $("sel-number").textContent = number;
  $("sel-name").textContent = name;
  renderRoster();
}

function updateSelectedDisplay() {
  $("sel-number").textContent = state.selected.number || "—";
  $("sel-name").textContent = state.selected.name || "—";
}

/* ---------- 小键盘 ---------- */
function pressKey(k) {
  if (k === "clear") {
    state.numInput = "";
  } else if (k === "back") {
    state.numInput = state.numInput.slice(0, -1);
  } else {
    if (state.numInput.length >= 20) return;
    state.numInput += k;
  }
  $("num-display").textContent = state.numInput || "";
  const found = state.roster.find((it) => it.number === state.numInput);
  state.selected = found ? { number: found.number, name: found.name } : { number: state.numInput, name: "" };
  updateSelectedDisplay();
  renderRoster();
}

/* ---------- 叫号 ---------- */
async function doCall(force) {
  const number = state.numInput || state.selected.number;
  if (!number) { toast("请先输入学号或选择学生"); return; }
  const note = $("note").value.trim();
  const teacher = state.teacher;
  const name = state.selected.name;
  // 按下叫号立即清空学号输入框（备注保留不清空）
  state.numInput = "";
  state.selected = { number: "", name: "" };
  $("num-display").textContent = "";
  updateSelectedDisplay();
  renderRoster();
  $("btn-call").disabled = true;
  try {
    const d = await api("/api/call", {
      method: "POST",
      body: JSON.stringify({ number, name, note, teacher, force: !!force }),
    });
    $("call-banner").textContent = d.phrase;
    $("call-banner").classList.remove("flash");
    void $("call-banner").offsetWidth;
    $("call-banner").classList.add("flash");
    $("call-count").textContent = d.call_count;
    toast("已叫号：" + d.name);
    loadCalls();
  } catch (e) {
    if (e.status === 409 && e.data && e.data.duplicate) {
      // 重复叫号弹窗确认
      if (confirm(e.message + "\n\n（5 分钟内同一位老师已叫过该学生）\n点“确定”继续叫号，点“取消”返回。")) {
        $("btn-call").disabled = false;
        return doCallForce(number, name, note, teacher);
      }
      toast("已取消叫号");
    } else {
      if (!maybeActivate(e)) toast("叫号失败：" + e.message);
    }
  } finally {
    $("btn-call").disabled = false;
  }
}

async function doCallForce(number, name, note, teacher) {
  $("btn-call").disabled = true;
  try {
    const d = await api("/api/call", {
      method: "POST",
      body: JSON.stringify({ number, name, note, teacher, force: true }),
    });
    $("call-banner").textContent = d.phrase;
    $("call-banner").classList.remove("flash");
    void $("call-banner").offsetWidth;
    $("call-banner").classList.add("flash");
    $("call-count").textContent = d.call_count;
    toast("已叫号：" + d.name);
    loadCalls();
  } catch (e) {
    if (!maybeActivate(e)) toast("叫号失败：" + e.message);
  } finally {
    $("btn-call").disabled = false;
  }
}

/* ---------- 文本消息 ---------- */
let lastMsgText = null;
async function sendMessage() {
  const text = $("msg-text").value.trim();
  if (!text) { toast("请输入消息内容"); return; }
  // 与上次相同内容时提示确认，避免误重复发送
  if (text === lastMsgText && !confirm("消息内容与上次相同，是否再次发送？")) {
    return;
  }
  try {
    await api("/api/message", {
      method: "POST",
      body: JSON.stringify({ text, teacher: state.teacher }),
    });
    lastMsgText = text;
    toast("消息已发送，电脑端将语音读出");
  } catch (e) {
    toast("发送失败：" + e.message);
  }
}

/* ---------- 设置 ---------- */
function fillSettingsForm() {
  const c = state.config;
  $("set-phrase").value = c.phrase || "请%number%号%name%到%teacher%办公室%note%";
  $("set-play-count").value = c.play_count || 1;
  $("set-volume-enabled").checked = c.volume_enabled !== false;
  $("set-volume").value = (c.volume == null ? 100 : c.volume);
  $("set-volume").disabled = !$("set-volume-enabled").checked;
  $("set-prompt-enabled").checked = !!c.prompt_enabled;
  $("set-prompt-sound").value = c.prompt_sound || "beep";
  $("set-tts-engine").value = c.tts_engine || "auto";
  $("set-autostart").checked = !!c.autostart;
  $("set-mdns-enabled").checked = !!c.mdns_enabled;
  if ($("set-port")) $("set-port").value = c.port || 8000;
  $("set-mdns-hostname").value = c.mdns_hostname || "";
  const gSel = $("set-gesture");
  if (gSel) gSel.value = c.remote_gesture_mode || "zoom_scroll";
  const rtSel = $("set-remote-touch");
  if (rtSel) rtSel.value = c.remote_touch_mode || "auto";
  const qSel = $("set-monitor-quality");
  if (qSel) qSel.value = c.monitor_quality || "standard";
  const ifSel = $("set-mdns-interface");
  ifSel.innerHTML = "";
  ifSel.add(new Option("自动（全部网卡）", "auto"));
  for (const it of (state.interfaces || [])) {
    ifSel.add(new Option(it.name + "（" + (it.ips || []).join(", ") + "）", it.id));
  }
  ifSel.value = c.mdns_interface || "auto";
  $("set-secure-port").value = c.secure_port || 9443;
  if ($("set-secure-force")) $("set-secure-force").checked = !!c.secure_port_force;
  $("set-secure-enabled").checked = c.secure_enabled !== false;
  if ($("set-creds-enabled")) $("set-creds-enabled").checked = !!c.creds_enabled;
  if ($("set-creds-slot")) $("set-creds-slot").value = c.creds_slot || "";
  if ($("set-creds-code")) $("set-creds-code").value = c.creds_code || "";
  if ($("set-creds-repo")) $("set-creds-repo").value = c.creds_repo || "";
  if ($("set-creds-poll")) $("set-creds-poll").value = c.creds_poll_minutes || 180;
  updatePhrasePreview();
}

function updatePhrasePreview() {
  $("phrase-preview").textContent = ($("set-phrase").value || "")
    .replace(/%number%/g, "2023001")
    .replace(/%name%/g, "张三")
    .replace(/%note%/g, "带上作业")
    .replace(/%teacher%/g, state.teacher || "王老师");
}

async function saveSettings() {
  const oldPort = state.config.port;
  const oldHost = String(state.config.mdns_hostname || "").toLowerCase();
  const cfg = {
    phrase: $("set-phrase").value.trim(),
    play_count: parseInt($("set-play-count").value, 10) || 1,
    volume_enabled: $("set-volume-enabled").checked,
    volume: Math.max(0, Math.min(100, parseInt($("set-volume").value, 10) || 100)),
    prompt_enabled: $("set-prompt-enabled").checked,
    prompt_sound: $("set-prompt-sound").value,
    tts_engine: $("set-tts-engine").value,
    autostart: $("set-autostart").checked,
    mdns_enabled: $("set-mdns-enabled").checked,
    mdns_hostname: $("set-mdns-hostname").value.trim(),
    port: Math.max(1, Math.min(65535, parseInt($("set-port").value, 10) || 8000)),
    mdns_interface: $("set-mdns-interface").value || "auto",
    monitor_quality: ($("set-monitor-quality") || {}).value || "standard",
    remote_touch_mode: ($("set-remote-touch") || {}).value || "auto",
    remote_gesture_mode: ($("set-gesture") || {}).value || "zoom_scroll",
    secure_port: parseInt($("set-secure-port").value, 10) || 9443,
    secure_port_force: !!($("set-secure-force") && $("set-secure-force").checked),
    secure_enabled: $("set-secure-enabled").checked,
  };
  try {
    // 端口变了先检查能不能绑：避免存进去一个用不了的端口（重启后才炸）
    if (cfg.port !== oldPort) {
      try {
        const r = await fetch("/api/port_check?port=" + encodeURIComponent(cfg.port));
        const chk = await r.json();
        if (chk && chk.ok === false) {
          toast("端口 " + cfg.port + " 用不了：" + (chk.reason || "未知原因") + "，换一个再保存");
          return;
        }
      } catch (e) { /* 校验接口本身异常时不阻塞保存 */ }
    }
    await authedApi("/api/settings", { method: "POST", body: JSON.stringify(cfg) });
    state.config = { ...state.config, ...cfg };
    // 教室端凭据单独存：激活码只留在本机 config.json，不进普通设置接口
    if ($("set-creds-enabled")) {
      try {
        await authedApi("/api/creds/config", {
          method: "POST",
          body: JSON.stringify({
            creds_enabled: $("set-creds-enabled").checked,
            creds_slot: ($("set-creds-slot").value || "").trim(),
            creds_code: ($("set-creds-code").value || "").trim(),
            creds_repo: ($("set-creds-repo").value || "").trim(),
            creds_poll_minutes: parseInt($("set-creds-poll").value, 10) || 180,
          }),
        });
      } catch (e) {
        toast("凭据设置保存失败：" + (e.message || "未知原因"));
      }
    }
    await loadStatus();
    const hostChanged = String(cfg.mdns_hostname || "").toLowerCase() !== oldHost;
    const tips = [];
    if (cfg.port !== oldPort) tips.push("端口需重启生效");
    if (hostChanged) tips.push("主机名已改：http 立刻可用，https 需重启一次生效");
    toast("设置已保存" + (tips.length ? "（" + tips.join("；") + "）" : ""));
  } catch (e) {
    if (e.code === "NEED_LOGIN" || /请先登录|未激活/.test(e.message)) {
      location.href = "/login";
    } else {
      toast("保存失败：" + e.message);
    }
  }
}

/* ---------- 重启软件 ---------- */
async function restartApp() {
  if (!confirm("确定重启软件吗？网页会短暂断开，约十几秒后自动恢复。")) return;
  try {
    await authedApi("/api/restart", { method: "POST", body: JSON.stringify({}) });
    toast("软件正在重启，网页即将断开…");
    setTimeout(() => { location.reload(); }, 3000);
  } catch (e) {
    if (e.code === "NEED_LOGIN" || /请先登录|未激活/.test(e.message)) {
      location.href = "/login";
    } else {
      toast("重启失败：" + e.message);
    }
  }
}

/* ---------- CSV 导入 ---------- */
async function uploadCsv(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("has_header", $("csv-has-header").checked ? "true" : "false");
  const data = await authedForm("/api/csv/upload", fd);
  state.csvFileId = data.file_id;
  state.csvColumns = data.columns;
  $("csv-step2").classList.remove("hidden");
  fillColumnSelects(data);
  renderCsvPreview(data);
  $("csv-summary").textContent = "共读取 " + data.total_rows + " 行数据";
}

function fillColumnSelects(data) {
  const numSel = $("csv-number-col");
  const nameSel = $("csv-name-col");
  numSel.innerHTML = "";
  nameSel.innerHTML = "";
  data.columns.forEach((col, i) => {
    const label = col || ("列" + (i + 1));
    numSel.add(new Option(label, i));
    nameSel.add(new Option(label, i));
  });
  if (data.suggest_number != null) numSel.value = data.suggest_number;
  if (data.suggest_name != null) nameSel.value = data.suggest_name;
}

function renderCsvPreview(data) {
  const tb = $("csv-preview-table");
  tb.innerHTML = "";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const col of data.columns) {
    const th = document.createElement("th");
    th.textContent = col || " ";
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  tb.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const row of data.preview) {
    const tr = document.createElement("tr");
    for (let i = 0; i < data.columns.length; i++) {
      const td = document.createElement("td");
      td.textContent = row[i] ?? "";
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  tb.appendChild(tbody);
}

async function doImportCsv() {
  if (!state.csvFileId) { toast("请先上传 CSV 文件"); return; }
  try {
    const d = await authedApi("/api/csv/import", {
      method: "POST",
      body: JSON.stringify({
        file_id: state.csvFileId,
        number_col: parseInt($("csv-number-col").value, 10),
        name_col: parseInt($("csv-name-col").value, 10),
        has_header: $("csv-has-header").checked,
        mode: $("csv-mode").value,
      }),
    });
    toast("导入成功：" + d.imported + " 条");
    closeModal("modal-import");
    $("csv-file").value = "";
    state.csvFileId = null;
    $("csv-step2").classList.add("hidden");
    await loadRoster();
    await loadStatus();
  } catch (e) {
    toast("导入失败：" + e.message);
  }
}

/* ---------- 名条管理 ---------- */
function renderManage() {
  const ul = $("manage-list");
  ul.innerHTML = "";
  const list = sortRoster(state.roster);
  for (const it of list) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="num">' + esc(it.number) + '</span><span class="nm">' + esc(it.name) +
      '</span><button class="del" data-number="' + esc(it.number) + '">删除</button>';
    li.querySelector(".del").addEventListener("click", () => deleteStudent(it.number));
    ul.appendChild(li);
  }
  $("manage-summary").textContent = "共 " + state.roster.length + " 条名条";
}

async function deleteStudent(number) {
  try {
    await authedApi("/api/roster/delete", { method: "POST", body: JSON.stringify({ number }) });
    toast("已删除 " + number);
    await loadRoster();
    renderManage();
  } catch (e) { if (!maybeActivate(e)) toast("删除失败：" + e.message); }
}

async function addStudent() {
  const number = $("add-number").value.trim();
  const name = $("add-name").value.trim();
  if (!number || !name) { toast("学号和姓名都不能为空"); return; }
  try {
    await api("/api/roster", { method: "POST", body: JSON.stringify({ number, name }) });
    $("add-number").value = "";
    $("add-name").value = "";
    toast("已添加 " + name);
    await loadRoster();
    renderManage();
  } catch (e) { toast("添加失败：" + e.message); }
}

async function clearAll() {
  if (!confirm("确定清空全部名条吗？此操作不可撤销。")) return;
  try {
    await authedApi("/api/roster/clear", { method: "POST", body: JSON.stringify({}) });
    toast("已清空");
    await loadRoster();
    renderManage();
  } catch (e) { if (!maybeActivate(e)) toast("清空失败：" + e.message); }
}

/* ---------- 提示音上传 ---------- */
async function uploadSound(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const data = await authedForm("/api/sound/upload", fd);
    state.config = data.config;
    fillSettingsForm();
    toast("提示音已上传并启用");
  } catch (e) {
    if (!maybeActivate(e)) toast("上传失败：" + e.message);
  }
}

/* ---------- 事件绑定 ---------- */
function bindEvents() {
  $("numpad").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-k]");
    if (btn) pressKey(btn.dataset.k);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (/^[0-9]$/.test(e.key)) pressKey(e.key);
    else if (e.key === "Backspace") pressKey("back");
    else if (e.key === "Escape" || e.key.toLowerCase() === "c") pressKey("clear");
    else if (e.key === "Enter") doCall();
  });
  $("btn-call").addEventListener("click", () => doCall());
  $("search").addEventListener("input", renderRoster);
  $("btn-refresh-calls").addEventListener("click", loadCalls);
  $("btn-clear-calls").addEventListener("click", async () => {
    if (!confirm("确定清空全部叫号记录吗？")) return;
    try {
      await api("/api/calls/clear", { method: "POST", body: JSON.stringify({}) });
      await loadCalls();
      toast("叫号记录已清空");
    } catch (e) { toast("清空失败：" + e.message); }
  });

  // 文本消息
  $("btn-send-msg").addEventListener("click", sendMessage);
  $("msg-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
  });

  // 老师名称（存 cookie，不受密码保护）
  $("btn-save-teacher").addEventListener("click", () => {
    state.teacher = $("teacher-name").value.trim();
    setCookie("teacher_name", state.teacher, 365);
    toast("老师名称已保存到本浏览器");
  });
  $("teacher-name").addEventListener("change", () => {
    state.teacher = $("teacher-name").value.trim();
    setCookie("teacher_name", state.teacher, 365);
  });

  // 顶部功能入口
  $("btn-monitor").addEventListener("click", () => { location.href = "/monitor"; });
  $("btn-remote").addEventListener("click", () => { location.href = "/remote"; });
  $("btn-files").addEventListener("click", () => { location.href = "/files"; });
  $("btn-account").addEventListener("click", () => { location.href = "/account"; });
  $("btn-admin").addEventListener("click", () => { location.href = "/admin"; });

  // 设置
  /* ---------- 全屏叫号 ---------- */
function syncFsClass() {
  const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
  document.body.classList.toggle("fs", on);
  const b = $("btn-fullscreen");
  if (b) b.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
}
async function toggleFullscreen() {
  try {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      await (document.exitFullscreen ? document.exitFullscreen() : Promise.resolve());
    } else {
      const el = document.documentElement;
      if (el.requestFullscreen) await el.requestFullscreen();
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
      else {
        // 浏览器不允许全屏时，退化成"隐藏顶栏的紧凑模式"
        document.body.classList.toggle("fs");
      }
    }
  } catch (e) {
    document.body.classList.toggle("fs");
  }
  syncFsClass();
}
document.addEventListener("fullscreenchange", syncFsClass);
document.addEventListener("webkitfullscreenchange", syncFsClass);
$("btn-fullscreen").addEventListener("click", toggleFullscreen);

$("btn-settings").addEventListener("click", () => {
    loadConfig().then(() => { fillSettingsForm(); openModal("modal-settings"); });
  });
  $("btn-update").addEventListener("click", () => { location.href = "/update"; });
  $("set-phrase").addEventListener("input", updatePhrasePreview);
  $("set-volume-enabled").addEventListener("change", () => {
    $("set-volume").disabled = !$("set-volume-enabled").checked;
  });
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("btn-restart").addEventListener("click", restartApp);
  $("set-sound-file").addEventListener("change", (e) => uploadSound(e.target.files[0]));

  // CSV
  $("btn-import").addEventListener("click", () => openModal("modal-import"));
  $("csv-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try { await uploadCsv(f); } catch (err) { toast(err.message); }
  });
  $("csv-has-header").addEventListener("change", () => {
    const f = $("csv-file").files[0];
    if (f) uploadCsv(f).catch((err) => toast(err.message));
  });
  $("btn-csv-import").addEventListener("click", doImportCsv);

  // 名条管理
  $("btn-manage").addEventListener("click", () => { renderManage(); openModal("modal-manage"); });
  $("btn-add").addEventListener("click", addStudent);
  $("btn-clear-all").addEventListener("click", clearAll);
}

/* ---------- 启动 ---------- */
async function init() {
  state.teacher = getCookie("teacher_name") || "";
  $("teacher-name").value = state.teacher;
  bindEvents();
  await Promise.all([loadStatus(), loadRoster(), loadCalls(), loadAuthMe()]);
  await loadConfig();
  fillSettingsForm();
  updateSelectedDisplay();
}
init();

/* ==========================================================================
 * 公网访问（微软 Dev Tunnels）
 * - 所有接口只在电脑本机可用（后端已限制），手机上点这些按钮会被拒绝
 * ========================================================================== */
(function () {
  let pollTimer = null;

  function setBadge(el, text, kind) {
    if (!el) return;
    el.textContent = text;
    el.className = "badge " + (kind || "gray");
  }

  function drawQR(text) {
    const box = $("tunnel-qr");
    if (!box) return;
    box.innerHTML = "";
    if (!text || typeof qrcode !== "function") return;
    try {
      const qr = qrcode(0, "M");
      qr.addData(text);
      qr.make();
      box.innerHTML = qr.createImgTag(4, 6);
    } catch (e) { /* 忽略：二维码失败不影响主流程 */ }
  }

  function renderStatus(d) {
    const t = (d && d.tunnel) || {};
    const loginEl = $("tunnel-login-state");
    const hostEl = $("tunnel-host-state");
    if (t.login_state === "ok" || t.login_user) {
      setBadge(loginEl, "已连接：" + (t.login_user || "GitHub"), "ok");
      $("tunnel-login-box").style.display = "none";
    } else if (t.login_state === "waiting") {
      setBadge(loginEl, "等待授权…", "warn");
      $("tunnel-login-box").style.display = "";
      if (t.user_code) $("tunnel-user-code").textContent = t.user_code;
    } else {
      setBadge(loginEl, "未连接", "gray");
    }
    const st = t.host_state || "stopped";
    const label = { stopped: "未开通", starting: "启动中…", running: "运行中", error: "出错" }[st] || st;
    setBadge(hostEl, label, st === "running" ? "ok" : (st === "error" ? "warn" : "gray"));

    if (t.url && st === "running") {
      $("tunnel-url-box").style.display = "";
      const a = $("tunnel-url");
      a.textContent = t.url;
      a.href = t.url;
      drawQR(t.url);
    } else {
      $("tunnel-url-box").style.display = "none";
    }
    if (t.last_error) $("tunnel-msg").textContent = "提示：" + t.last_error;
  }

  async function loadTunnelStatus() {
    try {
      const d = await authedApi("/api/tunnel/status");
      renderStatus(d);
      return d;
    } catch (e) {
      // 手机上调用会被 403 拒绝，静默即可
      return null;
    }
  }

  function startPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const d = await loadTunnelStatus();
      const t = (d && d.tunnel) || {};
      const done = t.login_state === "ok" && t.host_state !== "starting";
      if (done) { clearInterval(pollTimer); pollTimer = null; }
    }, 3000);
  }

  const btnLogin = $("btn-tunnel-login");
  if (btnLogin) btnLogin.addEventListener("click", async () => {
    $("tunnel-msg").textContent = "";
    btnLogin.disabled = true;
    try {
      const d = await authedApi("/api/tunnel/login/start", { method: "POST", body: "{}" });
      renderStatus(d);
      if (((d.tunnel || {}).login_state) === "waiting") {
        $("tunnel-msg").textContent = "已生成设备码，请按提示去 GitHub 授权（会自动检测结果）。";
      }
      startPoll();
    } catch (e) {
      $("tunnel-msg").textContent = e.message || "无法开始登录";
    } finally {
      btnLogin.disabled = false;
    }
  });

  const btnStart = $("btn-tunnel-start");
  if (btnStart) btnStart.addEventListener("click", async () => {
    $("tunnel-msg").textContent = "正在创建/启动隧道（首次会下载组件，约 23MB）…";
    btnStart.disabled = true;
    try {
      const tid = ($("set-tunnel-id") && $("set-tunnel-id").value || "").trim();
      const d = await authedApi("/api/tunnel/start", {
        method: "POST", body: JSON.stringify({ tunnel_id: tid })
      });
      renderStatus(d);
      startPoll();
    } catch (e) {
      $("tunnel-msg").textContent = e.message || "启动失败";
    } finally {
      btnStart.disabled = false;
    }
  });

  const btnStop = $("btn-tunnel-stop");
  if (btnStop) btnStop.addEventListener("click", async () => {
    btnStop.disabled = true;
    try {
      const d = await authedApi("/api/tunnel/stop", { method: "POST", body: "{}" });
      renderStatus(d);
      $("tunnel-msg").textContent = "已停止公网访问。";
    } catch (e) {
      $("tunnel-msg").textContent = e.message || "停止失败";
    } finally {
      btnStop.disabled = false;
    }
  });

  loadTunnelStatus();
})();

/* ==========================================================================
 * 教室端凭据（定时拉取）：状态展示 + 立即拉取
 * ========================================================================== */
(function () {
  const $c = (id) => document.getElementById(id);
  function setBadge(el, text, cls) {
    if (!el) return;
    el.textContent = text;
    el.className = "badge " + (cls || "gray");
  }
  const LABEL = {
    idle: ["未启用", "gray"], fetching: ["拉取中…", "warn"],
    ok: ["正常", "ok"], error: ["失败", "danger"],
  };

  async function renderCreds() {
    if (!$c("creds-state")) return;
    let d = null;
    try {
      const r = await fetch("/api/creds/status", { credentials: "same-origin" });
      d = await r.json();
    } catch (e) { return; }
    const c = (d && d.creds) || {};
    let lab = LABEL[c.state] || ["未知", "gray"];
    if (!c.enabled) lab = ["未启用", "gray"];
    else if (!c.configured) lab = ["待填写", "warn"];
    setBadge($c("creds-state"), lab[0], lab[1]);

    const box = $c("creds-info");
    if (!box) return;
    if (c.enabled && c.configured) {
      const parts = [];
      if (c.tunnel_id) parts.push("隧道 " + c.tunnel_id);
      if (c.url) parts.push("公网地址 " + c.url);
      if (c.expires_text) parts.push("令牌还剩 " + c.expires_text);
      if (c.interval_minutes) parts.push("每 " + c.interval_minutes + " 分钟拉一次");
      if (c.message) parts.push("⚠ " + c.message);
      box.textContent = parts.join("　·　");
      box.style.display = parts.length ? "" : "none";
    } else {
      box.style.display = "none";
    }
  }

  const btn = $c("btn-creds-refresh");
  if (btn) btn.addEventListener("click", async () => {
    const m = $c("creds-msg");
    if (m) m.textContent = "正在拉取…";
    try {
      const r = await fetch("/api/creds/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: "{}",
      });
      const d = await r.json();
      if (m) m.textContent = d.ok ? "拉取成功，令牌已更新"
                                  : ("拉取失败：" + (d.error || "未知原因"));
    } catch (e) {
      if (m) m.textContent = "拉取失败：" + (e.message || "网络异常");
    }
    renderCreds();
  });

  renderCreds();
  setInterval(renderCreds, 20000);
})();

/* ==========================================================================
 * 自动登录 + 隧道命名/管理（不用发布工具，全在这台电脑上做）
 * ========================================================================== */
(function () {
  const $a = (id) => document.getElementById(id);
  const LABEL = {
    idle: ["未登录", "gray"], running: ["登录中…", "warn"],
    ok: ["已登录", "ok"], error: ["登录失败", "danger"],
  };

  function msg(text, kind) {
    const el = $a("auto-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = kind === "err" ? "#b3261e" : (kind === "ok" ? "#12805c" : "");
  }
  function badge(el, text, cls) {
    if (!el) return;
    el.textContent = text;
    el.className = "badge " + (cls || "gray");
  }

  async function jget(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    return r.json();
  }
  async function jpost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  async function renderAuto() {
    if (!$a("auto-state")) return;
    let d = null;
    try { d = await jget("/api/tunnel/auto_status"); } catch (e) { return; }
    const a = (d && d.auto) || {};
    let lab = LABEL[a.state] || ["未知", "gray"];
    if (a.logged_in) lab = ["已登录：" + a.logged_in, "ok"];
    else if (a.state === "idle" && a.has_credentials) lab = ["待登录", "gray"];
    badge($a("auto-state"), lab[0], lab[1]);
    if ($a("set-gh-user") && !$a("set-gh-user").value && a.cred_user) {
      $a("set-gh-user").value = a.cred_user;
    }
    if (a.state === "error" && a.message) msg(a.message, "err");
  }

  const btnAuto = $a("btn-auto-login");
  if (btnAuto) btnAuto.addEventListener("click", async () => {
    const user = ($a("set-gh-user").value || "").trim();
    const pwd = $a("set-gh-pwd").value || "";
    msg("正在自动登录（大约 30~60 秒，别关页面）…");
    btnAuto.disabled = true;
    try {
      const r = await jpost("/api/tunnel/login_auto", { username: user, password: pwd });
      if (!r.ok) { msg(r.error || "启动失败", "err"); return; }
      if (r.message) { msg(r.message, "ok"); }
      const t = setInterval(async () => {
        const s = await jget("/api/tunnel/auto_status");
        const a = (s.auto) || {};
        if (a.state === "running") return;
        clearInterval(t);
        btnAuto.disabled = false;
        if (a.state === "ok") {
          msg("登录成功：" + (a.user || a.logged_in || ""), "ok");
          $a("set-gh-pwd").value = "";
        } else {
          msg(a.message || "登录失败", "err");
        }
        renderAuto();
        loadNames();
      }, 2500);
    } catch (e) {
      btnAuto.disabled = false;
      msg("出错：" + (e.message || "网络异常"), "err");
    }
  });

  /* ---------- 隧道命名 ---------- */
  function nameMsg(text, kind) {
    const el = $a("name-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = kind === "err" ? "#b3261e" : (kind === "ok" ? "#12805c" : "#6b7590");
  }

  const btnProbe = $a("btn-probe-name");
  if (btnProbe) btnProbe.addEventListener("click", async () => {
    const tid = ($a("set-tunnel-id").value || "").trim();
    if (!tid) { nameMsg("先填一个隧道名，例如 cc-101", "err"); return; }
    nameMsg("检查中…");
    const r = await jpost("/api/tunnel/probe_name", { tunnel_id: tid });
    if (!r.ok) { nameMsg(r.error || "检查失败", "err"); return; }
    const kind = r.state === "taken" ? "err" : (r.state === "free" ? "ok" : "");
    nameMsg(tid + " —— " + (r.label || ""), kind);
  });

  const btnCreate = $a("btn-create-name");
  if (btnCreate) btnCreate.addEventListener("click", async () => {
    const tid = ($a("set-tunnel-id").value || "").trim();
    if (!tid) { nameMsg("先填一个隧道名，例如 cc-101", "err"); return; }
    nameMsg("正在创建并开通…");
    const r = await jpost("/api/tunnel/create_async", { tunnel_id: tid });
    if (!r.ok) { nameMsg(r.error || "创建失败", "err"); return; }
    nameMsg("已" + (r.created ? "创建" : "复用") + "隧道 " + r.tunnel_id + "，正在开通公网访问…", "ok");
    loadNames();
  });

  const btnDel = $a("btn-del-name");
  if (btnDel) btnDel.addEventListener("click", async () => {
    const tid = ($a("set-tunnel-id").value || "").trim();
    if (!tid) { nameMsg("先填要删除的隧道名", "err"); return; }
    if (!confirm("删除隧道 \"" + tid + "\"？删掉后这个公网地址就失效了，手机要重新扫码。")) return;
    const r = await jpost("/api/tunnel/delete_name", { tunnel_id: tid });
    nameMsg(r.ok ? ("已删除 " + tid) : (r.error || "删除失败"), r.ok ? "ok" : "err");
    loadNames();
  });

  async function loadNames() {
    const box = $a("tunnel-names");
    if (!box) return;
    const r = await jget("/api/tunnel/list_names");
    box.innerHTML = "";
    if (!r.ok) { box.textContent = r.error || ""; return; }
    const cur = (r.current || "").trim();
    if (!$a("set-tunnel-id").value && cur) $a("set-tunnel-id").value = cur;
    const list = r.tunnels || [];
    if (!list.length) { box.textContent = "本账号还没有隧道，直接在上面填一个名字点「创建并使用」。"; return; }
    box.appendChild(document.createTextNode("本账号已有：")); 
    for (const t of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "badge " + (t === cur ? "ok" : "gray");
      b.style.cssText = "cursor:pointer;margin:0 6px 4px 0;border:0";
      b.textContent = t + (t === cur ? "（当前）" : "");
      b.addEventListener("click", () => { $a("set-tunnel-id").value = t; nameMsg("已选择 " + t); });
      box.appendChild(b);
    }
  }

  renderAuto();
  setTimeout(loadNames, 800);
  setInterval(renderAuto, 20000);
})();

/* ==========================================================================
 * 批量生成隧道名
 * ========================================================================== */
(function () {
  const $b = (id) => document.getElementById(id);
  const btn = $b("btn-batch-names");
  if (!btn) return;
  const NAMES = { created: "新建", reused: "复用", taken: "被占用", error: "失败" };

  btn.addEventListener("click", async () => {
    const box = $b("batch-result");
    box.textContent = "正在批量创建（每个约 2~5 秒，别关页面）…";
    btn.disabled = true;
    try {
      const r = await fetch("/api/tunnel/create_batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          prefix: ($b("set-tid-prefix").value || "cc-"),
          start: parseInt($b("set-tid-start").value, 10) || 1,
          end: parseInt($b("set-tid-end").value, 10) || 8,
        }),
      }).then((x) => x.json());
      if (!r.ok) { box.textContent = r.error || "失败"; return; }
      let html = "<div>共 " + r.total + " 个，成功 " + r.ok_count + " 个</div>" +
        "<table style='margin-top:6px'><thead><tr><th>隧道名</th><th>结果</th><th>说明</th></tr></thead><tbody>";
      for (const row of (r.rows || [])) {
        const color = row.state === "taken" || row.state === "error" ? "#b3261e"
                    : (row.state === "created" ? "#12805c" : "");
        html += "<tr><td style='font-family:monospace'>" + row.name + "</td>" +
          "<td style='color:" + color + "'>" + (NAMES[row.state] || row.state) + "</td>" +
          "<td>" + (row.message || "") + "</td></tr>";
      }
      box.innerHTML = html + "</tbody></table>";
    } catch (e) {
      box.textContent = "出错：" + (e.message || "网络异常");
    } finally {
      btn.disabled = false;
    }
  });
})();

/* ==========================================================================
 * 内网穿透：② 隧道管理列表框 + ③ 穿透策略开关 + ① 重新登录
 * ========================================================================== */
(function () {
  const $t = (id) => document.getElementById(id);
  const jget = (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json());
  const jpost = (u, b) => fetch(u, {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify(b || {}),
  }).then((r) => r.json());

  function note(text, kind) {
    const el = $t("name-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = kind === "err" ? "#b3261e" : (kind === "ok" ? "#12805c" : "");
  }

  /* ---------- ② 隧道列表 ---------- */
  async function renderTunnelList() {
    const box = $t("tunnel-list");
    if (!box) return;
    let d;
    try { d = await jget("/api/tunnel/list_names"); } catch (e) {
      box.textContent = "读取失败：" + (e.message || "网络异常");
      return;
    }
    box.innerHTML = "";
    if (!d.ok) { box.textContent = d.error || "读取失败"; return; }
    const cur = (d.current || "").trim();
    const list = d.tunnels || [];
    if (!list.length) {
      box.textContent = "还没有隧道。在下面填一个名字（如 cc-101）点「新建隧道」。";
      return;
    }
    const table = document.createElement("table");
    table.style.cssText = "width:100%;border-collapse:collapse;font-size:13.5px";
    table.innerHTML = "<thead><tr><th style='text-align:left'>隧道地址（名字）</th>" +
      "<th style='width:70px'>状态</th><th style='width:130px'>操作</th></tr></thead><tbody></tbody>";
    const tb = table.querySelector("tbody");
    for (const name of list) {
      const tr = document.createElement("tr");
      const isCur = name === cur;
      tr.innerHTML = "<td style='font-family:monospace;padding:5px 4px'>" + name +
        "</td><td>" + (isCur ? "<span class='badge ok'>当前</span>" : "") + "</td>" +
        "<td style='padding:5px 4px'></td>";
      const ops = tr.lastElementChild;
      const use = document.createElement("button");
      use.type = "button";
      use.className = "mini";
      use.textContent = "使用";
      use.addEventListener("click", async () => {
        $t("set-tunnel-id").value = name;
        const r = await jpost("/api/tunnel/create_async", { tunnel_id: name });
        note(r.ok ? ("已切到 " + name) : (r.error || "切换失败"), r.ok ? "ok" : "err");
        renderTunnelList();
      });
      ops.appendChild(use);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "mini danger";
      del.style.marginLeft = "6px";
      del.textContent = "删除";
      del.addEventListener("click", async () => {
        if (!confirm("删除隧道 " + name + "？地址会失效。")) return;
        const r = await jpost("/api/tunnel/delete_name", { tunnel_id: name });
        note(r.ok ? ("已删除 " + name) : (r.error || "删除失败"), r.ok ? "ok" : "err");
        renderTunnelList();
      });
      ops.appendChild(del);
      tb.appendChild(tr);
    }
    box.appendChild(table);
  }

  /* ---------- ③ 穿透策略 ---------- */
  async function syncStrategy() {
    const on = $t("chk-tunnel-on");
    const auto = $t("chk-tunnel-auto");
    if (!on || !auto) return;
    const d = await jget("/api/tunnel/status");
    const cfg = (STATE && STATE.config) || {};
    const st = d.tunnel || {};
    on.checked = !!(d.enabled || st.host_state === "running");
    auto.checked = cfg.tunnel_autostart !== false;
  }

  if ($t("chk-tunnel-on")) $t("chk-tunnel-on").addEventListener("change", async (e) => {
    const on = e.target.checked;
    const msg = $t("tunnel-msg");
    if (msg) msg.textContent = on ? "正在开启公网访问…" : "正在关闭…";
    const r = await jpost("/api/tunnel/enable_async", { on: on });
    if (msg) msg.textContent = r.ok ? (on ? "正在开启…" : "正在关闭…") : (r.error || "操作失败");
    if (!r.ok) e.target.checked = !on;
    else if (window.cqtAfterAction) {
      window.cqtAfterAction(on ? "已开启内网穿透" : "已关闭内网穿透");
    }
  });

  if ($t("chk-tunnel-auto")) $t("chk-tunnel-auto").addEventListener("change", async (e) => {
    const r = await jpost("/api/tunnel/autostart", { on: e.target.checked });
    if (!r.ok) { e.target.checked = !e.target.checked; }
  });

  /* ---------- ① 重新登录 ---------- */
  if ($t("btn-relogin")) $t("btn-relogin").addEventListener("click", async () => {
    const msg = $t("auto-msg");
    if (msg) msg.textContent = "正在强制重新登录（约 30~60 秒）…";
    const r = await jpost("/api/tunnel/relogin", {});
    if (!r.ok) { if (msg) msg.textContent = r.error || "失败"; return; }
    const t = setInterval(async () => {
      const s = await jget("/api/tunnel/auto_status");
      const a = s.auto || {};
      if (a.state === "running") return;
      clearInterval(t);
      if (msg) msg.textContent = a.state === "ok" ? ("已重新登录：" + (a.user || a.logged_in || ""))
                                                  : (a.message || "重新登录失败");
      renderTunnelList();
    }, 2500);
  });

  const openBtn = $t("btn-settings");
  if (openBtn) openBtn.addEventListener("click", () => {
    setTimeout(() => { renderTunnelList(); syncStrategy(); }, 600);
  });
  setTimeout(() => { renderTunnelList(); syncStrategy(); }, 1200);
})();

/* ---------- 公网地址/二维码：切换隧道或开关穿透后立即刷新 ---------- */
(function () {
  const $q = (id) => document.getElementById(id);

  function drawQr(text) {
    const box = $q("tunnel-qr");
    if (!box) return;
    if (!text || typeof qrcode !== "function") { box.innerHTML = ""; return; }
    try {
      const qr = qrcode(0, "M");
      qr.addData(text);
      qr.make();
      box.innerHTML = qr.createImgTag(4, 8);
    } catch (e) {
      box.textContent = text;
    }
  }

  async function refreshTunnelView() {
    let d;
    try {
      d = await fetch("/api/tunnel/status", { credentials: "same-origin" }).then((r) => r.json());
    } catch (e) { return ""; }
    const t = (d && d.tunnel) || {};
    const badge = $q("tunnel-host-state");
    if (badge) {
      const map = { running: ["运行中", "ok"], starting: ["启动中", "warn"],
                    error: ["出错", "danger"], stopped: ["未开启", "gray"] };
      const lab = map[t.host_state] || ["未开启", "gray"];
      badge.textContent = lab[0];
      badge.className = "badge " + lab[1];
    }
    const box = $q("tunnel-url-box");
    const link = $q("tunnel-url");
    if (t.url) {
      if (link) { link.textContent = t.url; link.href = t.url; }
      if (box) box.style.display = "";
      drawQr(t.url);
    } else {
      if (box) box.style.display = "none";
    }
    return t.url || "";
  }

  // 切换隧道 / 开关穿透之后：盯住地址变化，变了就重画二维码
  window.cqtRefreshTunnel = async function (oldUrl) {
    for (let i = 0; i < 25; i++) {
      const url = await refreshTunnelView();
      if (url && url !== oldUrl) return url;
      await new Promise((r) => setTimeout(r, 1500));
    }
    return "";
  };

  const openBtn = $q("btn-settings");
  if (openBtn) openBtn.addEventListener("click", () => setTimeout(refreshTunnelView, 500));
  setTimeout(refreshTunnelView, 1500);
})();


/* ---------- 统一收尾：等后台任务 → 刷新列表 → 刷新地址/二维码 ---------- */
(function () {
  const $g = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function drawQr(text) {
    const box = $g("tunnel-qr");
    if (!box) return;
    if (!text) { box.innerHTML = ""; return; }
    if (typeof qrcode !== "function") { box.textContent = text; return; }
    try {
      const qr = qrcode(0, "M");
      qr.addData(text);
      qr.make();
      box.innerHTML = qr.createImgTag(4, 8);
    } catch (e) { box.textContent = text; }
  }

  async function refreshView() {
    let d;
    try {
      d = await fetch("/api/tunnel/status", { credentials: "same-origin" }).then((r) => r.json());
    } catch (e) { return ""; }
    const t = (d && d.tunnel) || {};
    const badge = $g("tunnel-host-state");
    if (badge) {
      const m = { running: ["运行中", "ok"], starting: ["启动中", "warn"],
                  error: ["出错", "danger"], stopped: ["未开启", "gray"] };
      const l = m[t.host_state] || ["未开启", "gray"];
      badge.textContent = l[0];
      badge.className = "badge " + l[1];
    }
    // 开关跟着真实状态走（否则会出现"没勾但服务已经开着"）
    const cbOn = $g("chk-tunnel-on");
    if (cbOn) {
      cbOn.checked = !!(d.enabled || t.host_state === "running" || t.host_state === "starting");
    }
    const cbAuto = $g("chk-tunnel-auto");
    if (cbAuto && typeof d.autostart === "boolean") cbAuto.checked = d.autostart;

    const box = $g("tunnel-url-box");
    const link = $g("tunnel-url");
    if (t.url) {
      if (link) { link.textContent = t.url; link.href = t.url; }
      if (box) box.style.display = "";
      drawQr(t.url);
    } else {
      // 关掉穿透后：地址和二维码一起收起来，别留个点不开的旧链接
      if (box) box.style.display = "none";
      if (link) { link.textContent = ""; link.removeAttribute("href"); }
      drawQr("");
    }
    return t.url || "";
  }

  async function refreshList() {
    const box = $g("tunnel-list");
    if (!box) return;
    let d;
    try { d = await fetch("/api/tunnel/list_names", { credentials: "same-origin" }).then((r) => r.json()); }
    catch (e) { box.textContent = "读取失败：" + (e.message || ""); return; }
    if (!d.ok) { box.textContent = d.error || "读取失败"; return; }
    const cur = (d.current || "").trim();
    const list = d.tunnels || [];
    if (!list.length) { box.textContent = "还没有隧道。在下面填名字点「新建隧道」。"; return; }
    let html = "<table style='width:100%;border-collapse:collapse;font-size:13.5px'>" +
      "<thead><tr><th style='text-align:left'>隧道地址（名字）</th><th style='width:70px'>状态</th>" +
      "<th style='width:120px'>操作</th></tr></thead><tbody>";
    for (const n of list) {
      html += "<tr><td style='font-family:monospace;padding:5px 4px'>" + n + "</td><td>" +
        (n === cur ? "<span class='badge ok'>当前</span>" : "") + "</td>" +
        "<td style='padding:5px 4px'><button class='mini' data-use='" + n + "'>使用</button> " +
        "<button class='mini danger' data-del='" + n + "'>删除</button></td></tr>";
    }
    box.innerHTML = html + "</tbody></table>";
    box.querySelectorAll("button[data-use]").forEach((b) => {
      b.addEventListener("click", async () => {
        const name = b.getAttribute("data-use");
        $g("set-tunnel-id").value = name;
        const r = await fetch("/api/tunnel/create_async", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin", body: JSON.stringify({ tunnel_id: name }),
        }).then((x) => x.json());
        if (!r.ok) { note(r.error || "切换失败", "err"); return; }
        note("正在切到 " + name + "…");
        window.cqtAfterAction("已切换到 " + name);
      });
    });
    box.querySelectorAll("button[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        const name = b.getAttribute("data-del");
        if (!confirm("删除隧道 " + name + "？地址会失效。")) return;
        const r = await fetch("/api/tunnel/delete_name", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin", body: JSON.stringify({ tunnel_id: name }),
        }).then((x) => x.json());
        note(r.ok ? ("已删除 " + name) : (r.error || "删除失败"), r.ok ? "ok" : "err");
        setTimeout(refreshList, 300);     // 删除是同步的，马上刷
      });
    });
  }

  function note(text, kind) {
    const el = $g("name-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = kind === "err" ? "#b3261e" : (kind === "ok" ? "#12805c" : "#6b7590");
  }

  window.cqtAfterAction = async function (doneLabel) {
    // 等后台任务结束，并把失败原因显示出来
    for (let i = 0; i < 40; i++) {
      let job = {};
      try {
        const d = await fetch("/api/tunnel/job", { credentials: "same-origin" }).then((r) => r.json());
        job = d.job || {};
      } catch (e) { break; }
      if (!job.running) {
        if (!job.ok && job.error) note("操作失败：" + job.error, "err");
        else if (job.ok && doneLabel) note(doneLabel, "ok");
        break;
      }
      await sleep(1200);
    }
    await refreshList();
    // 地址可能要几秒才出来（隧道在连），最多盯 40 秒
    let last = "";
    for (let i = 0; i < 20; i++) {
      last = await refreshView();
      if (last) break;
      await sleep(2000);
    }
    if (last && doneLabel) note(doneLabel + "　" + last, "ok");
  };

  window.cqtRefreshAll = async function () {
    await refreshList();
    await refreshView();
  };

  const openBtn = $g("btn-settings");
  if (openBtn) openBtn.addEventListener("click", () => setTimeout(function () {
    refreshList(); refreshView();
  }, 600));
  setTimeout(function () { refreshList(); refreshView(); }, 1500);
})();

/* ==========================================================================
 * 右上角应用启动器（右侧滑出）
 * ========================================================================== */
(function () {
  const body = document.body;
  const btn = document.getElementById("btn-apps");
  const mask = document.getElementById("drawer-mask");
  const closeBtn = document.getElementById("drawer-close");
  const open = () => body.classList.add("drawer-open");
  const close = () => body.classList.remove("drawer-open");

  if (btn) btn.addEventListener("click", () => {
    body.classList.contains("drawer-open") ? close() : open();
  });
  if (mask) mask.addEventListener("click", close);
  if (closeBtn) closeBtn.addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  // 用事件委托：抽屉的 HTML 在脚本之后才出现，直接绑会绑不到
  document.addEventListener("click", (ev) => {
    const item = ev.target && ev.target.closest ? ev.target.closest(".app-item") : null;
    if (!item) return;
      const target = document.getElementById(item.dataset.target);
      const group = item.dataset.group || "";
      close();
      // 设置类入口直接点，避免 setTimeout 在关闭动画里被吞掉
      if (target && item.dataset.target === "btn-settings") {
        try { target.click(); } catch (e) { console.error(e); }
      }
      setTimeout(() => {
        if (target && item.dataset.target !== "btn-settings") {
          target.click();
          // 兜底：万一按钮的绑定失效，直接打开对应弹窗
          const map = { "btn-manage": "modal-manage", "btn-files": "modal-files" };
          const mid = map[item.dataset.target];
          if (mid) {
            setTimeout(() => {
              const m = document.getElementById(mid);
              if (m && m.classList.contains("hidden") && typeof openModal === "function") {
                openModal(mid);
              }
            }, 450);
          }
        }
        if (!group) return;
        // 打开设置有读取配置的延迟，轮询等它真的出现，再滚到对应分组
        let tries = 0;
        const timer = setInterval(() => {
          tries += 1;
          const modal = document.getElementById("modal-settings");
          const shown = modal && !modal.classList.contains("hidden");
          if (!shown) {
            if (tries > 60) clearInterval(timer);
            return;
          }
          const heads = modal.querySelectorAll(".set-group");
          let hit = null;
          heads.forEach((h) => { if (h.textContent.indexOf(group) >= 0) hit = h; });
          if (!hit) { if (tries > 60) clearInterval(timer); return; }
          clearInterval(timer);
          hit.scrollIntoView({ block: "start", behavior: "smooth" });
          hit.style.transition = "color .3s";
          hit.style.color = "#12805c";
          setTimeout(() => { hit.style.color = ""; }, 1200);
        }, 200);
      }, 130);
  });

  async function showAccountName() {
    try {
      const d = await fetch("/api/auth/session", { credentials: "same-origin" }).then((r) => r.json());
      const u = d.user || {};
      const el = document.getElementById("account-name");
      if (el) el.textContent = u.display_name || u.username || "我的账号";
    } catch (e) { /* 未登录时保持默认 */ }
  }
  showAccountName();
  setInterval(showAccountName, 15000);
})();


/* 抽屉顶端的"外网地址" */
(async function () {
  async function fillPublic() {
    const el = document.getElementById("drawer-public-url");
    if (!el) return;
    try {
      const d = await fetch("/api/tunnel/status", { credentials: "same-origin" }).then((r) => r.json());
      const t = (d && d.tunnel) || {};
      if (t.url) {
        el.innerHTML = '<a href="' + t.url + '" target="_blank" rel="noreferrer">' + t.url + "</a>";
      } else {
        el.textContent = d && d.enabled ? "启动中…" : "未开启";
      }
    } catch (e) { el.textContent = "未开启"; }
  }
  const btn = document.getElementById("btn-apps");
  if (btn) btn.addEventListener("click", fillPublic);
  fillPublic();
  setInterval(fillPublic, 20000);
})();
