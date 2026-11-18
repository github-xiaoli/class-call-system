/* 软件更新页面逻辑 */
"use strict";
const $ = (id) => document.getElementById(id);
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(url, options = {}) {
  const opts = Object.assign({}, options);
  // 字符串 body 必须是 JSON，否则后端 get_json 取不到数据（会导致保存无效）
  if (typeof opts.body === "string" && !(opts.headers && opts.headers["Content-Type"])) {
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  }
  const res = await Auth.fetchAuthed(url, opts);
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) {
    const e = new Error(data.error || ("请求失败 " + res.status));
    e.status = res.status;
    if (res.status === 401) showAuthNotice();
    throw e;
  }
  return data;
}

let latestInfo = null;
let pollTimer = null;

const CHANNEL_NAMES = { stable: "稳定", beta: "内测", dev: "开发" };
const channelName = (c) => CHANNEL_NAMES[c] || c || "稳定";
const POLICY_NAMES = {
  none: "不检查更新", check: "启动时检查更新",
  download: "自动下载不安装", install: "自动安装",
};
const policyName = (p) => POLICY_NAMES[p] || p || "不检查更新";

async function loadConfig() {
  try {
    const d = await api("/api/update/config");
    $("cur-version").textContent = d.current_version || "?";
    $("cur-channel").textContent = channelName(d.update_channel);
    $("set-channel").value = d.update_channel || "stable";
    $("set-policy").value = d.update_policy || "none";
  } catch (e) {
    if (e.status !== 401) toast("加载配置失败：" + e.message);
  }
}

async function checkUpdate() {
  try {
    $("latest-info").textContent = "正在检查更新…";
    const d = await api("/api/update/check");
    if (!d.latest) {
      latestInfo = null;
      $("btn-update").classList.add("hidden");
      const remote = d.remote ? d.remote.version : "";
      $("latest-info").textContent = "已是最新版本（当前 " + (d.current || "?") +
        "，通道：" + channelName(d.channel) + "）" +
        (remote && remote !== d.current ? "，远端 " + remote : "");
      $("update-notes").textContent = "无可用更新";
      return;
    }
    latestInfo = d.latest;
    $("latest-info").textContent = "发现新版本：" + d.latest.version +
      "（当前 " + (d.current || "?") + "，通道：" + channelName(d.latest.channel) + "）";
    $("btn-update").classList.remove("hidden");
    renderNotes(d.latest.notes);
  } catch (e) { toast("检查失败：" + e.message); }
}

function renderNotes(notes) {
  const box = $("update-notes");
  box.textContent = "";
  if (!notes) { box.textContent = "无更新日志"; return; }
  for (const line of String(notes).split(/\r?\n/)) {
    const div = document.createElement("div");
    div.textContent = line;
    box.appendChild(div);
  }
}

let uiBusy = false;

function setBusy(b) {
  uiBusy = b;
  ["btn-update", "btn-install", "btn-check", "btn-upload"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = b;
  });
}

/* 更新进行中：整页蒙层锁死，只显示进度，避免重复点击/重复下载 */
function lockUI(title, detail) {
  const m = $("busy-mask");
  if (!m) return;
  $("busy-title").textContent = title || "正在下载更新";
  $("busy-detail").textContent = detail || "正在准备…";
  $("busy-fill").style.width = "0%";
  $("busy-pct").textContent = "0%";
  m.classList.remove("hidden");
  document.body.classList.add("ui-locked");
}

function unlockUI() {
  const m = $("busy-mask");
  if (m) m.classList.add("hidden");
  document.body.classList.remove("ui-locked");
}

/* 进度显示：下载阶段保留一位小数（不会出现 0/16/32/48 这种跳变） */
function pctText(v, stage) {
  const n = Number(v) || 0;
  const frac = ["download", "speedtest", "extract", "final"].indexOf(stage) >= 0;
  return (frac && n < 100 ? n.toFixed(1) : String(Math.round(n))) + "%";
}

function paintBusy(p) {
  const m = $("busy-mask");
  if (!m || m.classList.contains("hidden")) return;
  $("busy-title").textContent = stageText(p.stage);
  $("busy-fill").style.width = (Number(p.percent) || 0) + "%";
  $("busy-pct").textContent = pctText(p.percent, p.stage);
  $("busy-detail").textContent = p.detail || "";
}

async function startUpdate() {
  if (uiBusy) { toast("正在处理中，请稍候…"); return; }
  const v = latestInfo ? latestInfo.version : "最新版本";
  if (!confirm("确定下载 " + v + " 的更新包吗？下载解压完成后需要你再点一次「安装并重启」。")) return;
  setBusy(true);
  showProgress();
  lockUI("正在检查更新", "正在准备下载…");
  try {
    await api("/api/update/start", { method: "POST", body: JSON.stringify({}) });
    startPolling();
  } catch (e) {
    if (e.status === 409) {
      // 服务器拒绝重复的下载请求：保持锁定并接着看已有任务的进度
      toast("更新已在进行中，正在显示进度…");
      startPolling();
      return;
    }
    setBusy(false);
    unlockUI();
    toast("开始下载失败：" + e.message);
  }
}

async function installUpdate() {
  if (uiBusy) { toast("正在处理中，请稍候…"); return; }
  if (!confirm("立即安装并重启吗？重启期间网页会短暂断开。")) return;
  setBusy(true);
  showProgress();
  lockUI("正在安装", "正在切换版本，重启期间网页会短暂断开…");
  try {
    await api("/api/update/install", { method: "POST", body: JSON.stringify({}) });
    $("btn-install").classList.add("hidden");
    startPolling();
  } catch (e) {
    setBusy(false);
    unlockUI();
    toast("安装失败：" + e.message);
  }
}

function showProgress() {
  $("progress-panel").style.display = "";
  $("progress-fill").style.width = "0%";
  $("progress-stage").textContent = "准备中";
  $("progress-pct").textContent = "0%";
}

const ACTIVE_STAGES = ["start", "check", "speedtest", "download", "extract", "final"];

function renderProgress(p) {
  p = p || {};
  const active = !!p.running || ACTIVE_STAGES.indexOf(p.stage) >= 0;
  if (active) lockUI(stageText(p.stage), p.detail || "");
  $("progress-fill").style.width = (p.percent || 0) + "%";
  $("progress-pct").textContent = pctText(p.percent, p.stage);
  $("progress-stage").textContent = stageText(p.stage);
  $("progress-detail").textContent = p.detail || "";
  paintBusy(p);
  // 服务端不再"运行中"时解除按钮禁用，避免连续点击产生多次任务
  if (!p.running) setBusy(false);
  // 下载解压完成 -> 显示「安装并重启」按钮
  if (p.ready || p.stage === "ready") {
    const v = p.ready_version || p.version || "";
    $("btn-install").textContent = v ? ("✅ 安装并重启（" + v + "）") : "✅ 安装并重启";
    $("btn-install").classList.remove("hidden");
    unlockUI();
  } else if (!p.running && ACTIVE_STAGES.indexOf(p.stage) < 0) {
    unlockUI();   // 出错/空闲：解除锁定，允许重试
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startPolling() {
  stopPolling();
  const tick = async () => {
    try {
      const d = await api("/api/update/progress");
      const p = d.progress || {};
      renderProgress(p);
      const active = !!p.running || ACTIVE_STAGES.indexOf(p.stage) >= 0;
      if (!active) {
        stopPolling();
        if (p.stage === "error") {
          toast("更新失败：" + (p.error || p.detail || "未知错误"));
        } else if (p.stage === "done" && (p.percent || 0) >= 100) {
          toast("更新完成，正在重启…");
          setTimeout(() => location.reload(), 8000);
        } else if (p.detail) {
          toast(p.detail);
        }
      }
    } catch (e) { /* 忽略轮询瞬时错误（重启期间会短暂失败） */ }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

/* 进入页面时如果更新正在进行/已下载待安装，直接恢复显示（刷新也不丢） */
async function resumeProgress() {
  try {
    const d = await api("/api/update/progress");
    const p = d.progress || {};
    const active = !!p.running || ACTIVE_STAGES.indexOf(p.stage) >= 0;
    if (p.ready) {
      showProgress();
      renderProgress(p);
      if (active) startPolling();
      return;
    }
    if (!active) return;
    showProgress();
    renderProgress(p);
    startPolling();
  } catch (e) { /* 忽略 */ }
}

function stageText(stage) {
  const m = {
    idle: "空闲", check: "检查更新", speedtest: "测速选择线路",
    download: "下载更新包", extract: "解压安装", ready: "已下载，待安装",
    final: "正在安装", done: "完成", error: "失败", start: "准备中",
  };
  return m[stage] || stage || "…";
}

async function uploadZip(file) {
  if (!file) return;
  if (uiBusy) { toast("正在处理中，请稍候…"); return; }
  if (!confirm("确定用本地 zip 覆盖更新吗？更新完成后会自动重启。")) return;
  setBusy(true);
  lockUI("正在上传更新包", "正在把本地 zip 上传到软件…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await Auth.fetchAuthed("/api/update/upload", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || "上传失败");
    toast("已开始安装手动更新包");
    showProgress();
    setTimeout(startPolling, 400);
  } catch (e) { unlockUI(); toast("上传失败：" + e.message); }
}

async function savePolicy() {
  const ch = $("set-channel").value;
  const pol = $("set-policy").value;
  try {
    const d = await api("/api/update/config", {
      method: "POST",
      body: JSON.stringify({ update_channel: ch, update_policy: pol }),
    });
    toast("已保存：" + channelName(d.update_channel || ch) + " / " + policyName(d.update_policy || pol));
    $("menu-pop").classList.add("hidden");
    await loadConfig();
    checkUpdate();
  } catch (e) { toast("保存失败：" + e.message); }
}

/* 事件 */
$("btn-check").addEventListener("click", checkUpdate);
$("btn-update").addEventListener("click", startUpdate);
$("btn-install").addEventListener("click", installUpdate);
$("btn-upload").addEventListener("click", () => {
  const f = $("zip-file").files[0];
  if (f) uploadZip(f); else toast("请先选择 zip 文件");
});
$("btn-menu").addEventListener("click", () => {
  $("menu-pop").classList.toggle("hidden");
});
$("btn-save-policy").addEventListener("click", savePolicy);
document.addEventListener("click", (e) => {
  if (!e.target.closest("#menu-pop") && !e.target.closest("#btn-menu")) {
    $("menu-pop").classList.add("hidden");
  }
});

/* 授权检查：仅本机或已激活设备可用 */
async function ensureAuthorized() {
  try {
    const me = await Auth.me();
    if (me && (me.local || me.activated)) return true;
  } catch (e) { /* 继续按未授权处理 */ }
  showAuthNotice();
  return false;
}

function showAuthNotice() {
  $("auth-notice").classList.remove("hidden");
  const panels = document.querySelectorAll("main.page-wrap > section.panel");
  panels.forEach((p) => { if (p.id !== "auth-notice") p.classList.add("hidden"); });
  $("btn-menu").classList.add("hidden");
}

loadConfig();
ensureAuthorized().then((ok) => {
  if (!ok) return;
  resumeProgress().then(() => { if (!pollTimer) checkUpdate(); });
});
