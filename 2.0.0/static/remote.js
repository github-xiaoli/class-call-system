/* 远程控制：自动识别 电脑/手机 模式，WebSocket 低延迟画面 + 输入 */
"use strict";
const $ = (id) => document.getElementById(id);
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}
function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

let ws = null;
let started = false;
let drawing = false;
let pendingFrame = null;
let screenW = 1920;
let screenH = 1080;
let lastTouchSent = 0;
let lastTouchX = 0;
let lastTouchY = 0;
let mode = (("ontouchstart" in window) || navigator.maxTouchPoints > 0) ? "mobile" : "pc";

const screenBox = $("screen-box");
const canvas = $("screen");
const ctx = canvas.getContext("2d");

function handleAuthError(e) {
  if (e && (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活|请先激活/.test(e.message || ""))) {
    toast("请先激活");
    if (confirm("当前设备未激活。是否返回主页完成激活？")) location.href = "/";
    return false;
  }
  return true;
}

function openWs(path) {
  const url = new URL(path, location.href);
  url.protocol = (url.protocol === "https:" || url.protocol === "wss:") ? "wss:" : "ws:";
  return new WebSocket(url.href);
}

function applyMode() {
  const isPc = (mode === "pc");
  $("btn-mode").textContent = isPc ? "📱 手机模式" : "🖥 电脑模式";
  screenBox.classList.toggle("pc-mode", isPc);
  $("mode-hint").textContent = isPc
    ? "电脑模式：鼠标直接控制，键盘按键直接发送到被控电脑（点击画面后生效）。"
    : "手机模式：手指直接触控，等同在被控电脑上触摸。";
}
$("btn-mode").addEventListener("click", () => { mode = (mode === "pc" ? "mobile" : "pc"); applyMode(); });
applyMode();

function sendInput(payload) {
  if (ws && ws.readyState === 1) {
    try { ws.send(JSON.stringify(payload)); return; } catch (e) { /* 回退 HTTP */ }
  }
  Auth.fetchAuthed("/api/remote/input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then((res) => {
    if (!res.ok) res.json().then((d) => {
      if (!/远程控制未开启/.test(d.error || "")) toast(d.error || "输入失败");
    }).catch(() => {});
  }).catch((e) => { if (handleAuthError(e)) toast("发送失败：" + e.message); });
}

function drawFrame(buf) {
  drawing = true;
  const done = () => {
    drawing = false;
    // 有排队的帧就接着画最新的一张（只保留最新，避免越积越卡）
    if (pendingFrame) {
      const next = pendingFrame;
      pendingFrame = null;
      drawFrame(next);
    }
  };
  const blob = new Blob([buf], { type: "image/jpeg" });
  if (window.createImageBitmap) {
    createImageBitmap(blob).then((bmp) => {
      canvas.width = bmp.width;
      canvas.height = bmp.height;
      ctx.drawImage(bmp, 0, 0);
      bmp.close();
      $("screen-hint").classList.add("hidden");
      done();
    }).catch(done);
  } else {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      canvas.width = img.width;
      canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      $("screen-hint").classList.add("hidden");
      done();
    };
    img.onerror = done;
    img.src = url;
  }
}

async function startRemote() {
  try {
    const teacher = getCookie("teacher_name") || "老师";
    try {
      const res = await Auth.fetchAuthed("/api/remote/screen_size");
      const sd = await res.json();
      if (sd.ok && sd.width > 0) { screenW = sd.width; screenH = sd.height; }
    } catch (e) { /* 用默认分辨率 */ }

    const url = await Auth.urlWithToken("/ws/remote?teacher=" + encodeURIComponent(teacher));
    ws = openWs(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      started = true;
      $("remote-state").textContent = "已连接";
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") return;
      // 解码跟不上就直接丢帧（否则会积压，表现为每隔几秒卡一下）
      if (drawing) { pendingFrame = e.data; return; }
      drawFrame(e.data);
    };
    ws.onerror = () => { $("remote-state").textContent = "连接失败"; };
    ws.onclose = () => {
      started = false;
      $("remote-state").textContent = "已断开";
      $("screen-hint").textContent = "连接已断开，请刷新页面";
      $("screen-hint").classList.remove("hidden");
    };
  } catch (e) {
    $("remote-state").textContent = "连接失败";
    if (handleAuthError(e)) toast(e.message);
  }
}

function stopRemote() {
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  started = false;
}

/* ---------- 坐标映射 ---------- */
function clientToAbs(cx, cy) {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const x = (cx - rect.left) / rect.width * screenW;
  const y = (cy - rect.top) / rect.height * screenH;
  return {
    x: Math.max(0, Math.min(screenW, Math.round(x))),
    y: Math.max(0, Math.min(screenH, Math.round(y))),
  };
}

/* ---------- 手机模式：多指触摸注入 ---------- */
const pointerIds = new Map();   // touch.identifier -> Windows pointerId(1..10)
let nextPointerId = 1;

function pointerIdOf(id) {
  if (!pointerIds.has(id)) {
    if (nextPointerId > 10) nextPointerId = 1;
    pointerIds.set(id, nextPointerId++);
  }
  return pointerIds.get(id);
}

const activeTouches = new Map();  // touch.identifier -> { x, y, sent }

function touchToAbs(t) {
  return clientToAbs(t.clientX, t.clientY);
}

screenBox.addEventListener("touchstart", (e) => {
  if (mode !== "mobile") return;
  e.preventDefault();
  for (const t of e.changedTouches) {
    const p = touchToAbs(t);
    if (!p) continue;
    activeTouches.set(t.identifier, { x: p.x, y: p.y, sent: Date.now() });
    sendInput({ action: "touch", type: "down", x: p.x, y: p.y,
                pointer_id: pointerIdOf(t.identifier) });
  }
}, { passive: false });

screenBox.addEventListener("touchmove", (e) => {
  if (mode !== "mobile") return;
  e.preventDefault();
  const now = Date.now();
  for (const t of e.changedTouches) {
    const st = activeTouches.get(t.identifier);
    if (!st) continue;
    if (now - st.sent < 22) continue;   // 每根手指独立节流
    st.sent = now;
    const p = touchToAbs(t);
    if (!p) continue;
    st.x = p.x; st.y = p.y;
    sendInput({ action: "touch", type: "move", x: p.x, y: p.y,
                pointer_id: pointerIdOf(t.identifier) });
  }
}, { passive: false });

function endTouches(e) {
  if (mode !== "mobile") return;
  e.preventDefault();
  for (const t of e.changedTouches) {
    const st = activeTouches.get(t.identifier);
    if (!st) continue;
    sendInput({ action: "touch", type: "up", x: st.x, y: st.y,
                pointer_id: pointerIdOf(t.identifier) });
    activeTouches.delete(t.identifier);
    pointerIds.delete(t.identifier);
  }
}
screenBox.addEventListener("touchend", endTouches, { passive: false });
screenBox.addEventListener("touchcancel", endTouches, { passive: false });

/* ---------- 电脑模式：鼠标 + 键盘直接发送 ---------- */
screenBox.addEventListener("mousemove", (e) => {
  if (mode !== "pc") return;
  const p = clientToAbs(e.clientX, e.clientY);
  if (p) sendInput({ action: "absmove", x: p.x, y: p.y });
});
screenBox.addEventListener("mousedown", (e) => {
  if (mode !== "pc") return;
  if (e.button !== 0) return;
  const p = clientToAbs(e.clientX, e.clientY);
  if (p) sendInput({ action: "absmove", x: p.x, y: p.y });
  sendInput({ action: "mousedown", button: "left" });
});
screenBox.addEventListener("mouseup", (e) => {
  if (mode !== "pc") return;
  if (e.button !== 0) return;
  sendInput({ action: "mouseup", button: "left" });
});
screenBox.addEventListener("contextmenu", (e) => {
  if (mode !== "pc") return;
  e.preventDefault();
  sendInput({ action: "click", button: "right" });
});

function isEditable(target) {
  const tag = (target.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
}
document.addEventListener("keydown", (e) => {
  if (mode !== "pc" || !started) return;
  if (isEditable(e.target)) return;
  e.preventDefault();
  sendInput({ action: "keydown", key: e.key });
});
document.addEventListener("keyup", (e) => {
  if (mode !== "pc" || !started) return;
  if (isEditable(e.target)) return;
  e.preventDefault();
  sendInput({ action: "keyup", key: e.key });
});

/* ---------- OSK / 全屏 ---------- */
$("btn-osk").addEventListener("click", () => sendInput({ action: "osk" }));

$("btn-fullscreen").addEventListener("click", () => {
  if (!document.fullscreenElement) {
    (document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen || function () {}).call(document.documentElement);
  } else {
    (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
  }
});

window.addEventListener("beforeunload", stopRemote);
window.addEventListener("pagehide", stopRemote);

startRemote();
