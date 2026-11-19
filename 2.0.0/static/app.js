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
  activated: false,
  hasValidPassword: false,
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
  if (e && (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活/.test(e.message || ""))) {
    openModal("modal-activate");
    return true;
  }
  return false;
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
    const d = await Auth.me();
    state.isLocal = !!d.local;
    state.activated = !!d.activated;
    state.hasValidPassword = !!d.has_valid_password;
  } catch (e) { /* 忽略 */ }
  // 右上角按钮：本机显示“密码管理”；远程显示“激活/取消激活”
  $("btn-password").classList.toggle("hidden", !state.isLocal);
  $("btn-activate").classList.toggle("hidden", state.isLocal);
  $("btn-deactivate").classList.toggle("hidden", state.isLocal || !state.activated);
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
      toast("叫号失败：" + e.message);
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
    toast("叫号失败：" + e.message);
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
    await loadStatus();
    const hostChanged = String(cfg.mdns_hostname || "").toLowerCase() !== oldHost;
    const tips = [];
    if (cfg.port !== oldPort) tips.push("端口需重启生效");
    if (hostChanged) tips.push("主机名已改：http 立刻可用，https 需重启一次生效");
    toast("设置已保存" + (tips.length ? "（" + tips.join("；") + "）" : ""));
  } catch (e) {
    if (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活/.test(e.message)) {
      openModal("modal-activate");
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
    if (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活/.test(e.message)) {
      openModal("modal-activate");
    } else {
      toast("重启失败：" + e.message);
    }
  }
}

/* ---------- 激活 ---------- */
function openActivate() {
  $("act-password").value = "";
  $("act-status").textContent = "";
  if (!window.crypto || !crypto.subtle) {
    $("act-status").innerHTML = '当前不是安全连接，无法使用浏览器加密。请用手机浏览器打开：<br><b>' +
      esc(state.secureUrl || "https://本机地址:8443") + '</b><br>首次会提示证书不受信任，请点“继续访问”。';
  }
  openModal("modal-activate");
}

async function doActivate() {
  const password = $("act-password").value.trim();
  if (!password) { $("act-status").textContent = "请输入密码"; return; }
  $("act-status").textContent = "正在生成密钥并激活…";
  try {
    await Auth.activate(password);
    $("act-status").textContent = "✅ 激活成功，本设备已绑定。";
    await loadAuthMe();
    toast("激活成功");
    setTimeout(() => closeModal("modal-activate"), 800);
  } catch (e) {
    $("act-status").textContent = "激活失败：" + e.message;
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
  $("btn-password").addEventListener("click", () => { location.href = "/password"; });
  $("btn-activate").addEventListener("click", openActivate);
  $("btn-activate-do").addEventListener("click", doActivate);
  $("btn-deactivate").addEventListener("click", async () => {
    if (!confirm("取消激活后，本设备将无法再使用监控/远程控制/文件传输/设置等敏感功能，需重新激活。确定取消？")) return;
    await Auth.deactivate();
    await loadAuthMe();
    toast("已取消激活");
  });

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
