/* 远程监控 + 对讲（WebSocket 低延迟） */
"use strict";
const $ = (id) => document.getElementById(id);
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}

let audioCtx = null;
let playRate = 48000;      // 服务端上报的真实采样率
let pcmLowCount = 0;       // 连续低电平帧计数（用于提示没声音）
let nextTime = 0;
let camWs = null;
let micWs = null;
let monitoring = false;
let lastCamUrl = null;
let videoBusy = false;
let pcmChunks = [];
let pcmTotal = 0;

let talkCtx = null;
let talkStream = null;
let talkProc = null;
let talkWs = null;
let talkChunks = [];
let talking = false;

function handleAuthError(e) {
  if (e && (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活|请先激活/.test(e.message || ""))) {
    toast("请先激活：" + e.message);
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

function saveDeviceChoice(key, sel) {
  try {
    if (!sel || sel.selectedIndex < 0) return;
    const opt = sel.options[sel.selectedIndex];
    localStorage.setItem(key, JSON.stringify({ name: opt.text, value: opt.value }));
  } catch (e) { /* 忽略 */ }
}

function restoreDeviceChoice(key, sel) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw || !sel) return;
    const d = JSON.parse(raw);
    // 优先按名称匹配（设备重插后索引可能变化）
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].text === d.name) { sel.value = sel.options[i].value; return; }
    }
    for (let i = 0; i < sel.options.length; i++) {
      if (String(sel.options[i].value) === String(d.value)) { sel.value = sel.options[i].value; return; }
    }
  } catch (e) { /* 忽略 */ }
}

async function loadDevices() {
  try {
    const res = await Auth.fetchAuthed("/api/monitor/cameras");
    const cd = await res.json();
    const sel = $("cam-select");
    sel.innerHTML = "";
    for (const c of cd.cameras || []) sel.add(new Option(c.name, c.index));
    if (!sel.options.length) sel.add(new Option("未检测到摄像头", ""));
    restoreDeviceChoice("cqt_monitor_cam", sel);
  } catch (e) { toast("加载摄像头失败：" + e.message); }
  try {
    const res = await Auth.fetchAuthed("/api/monitor/mics");
    const md = await res.json();
    const sel = $("mic-select");
    sel.innerHTML = "";
    for (const m of md.mics || []) sel.add(new Option(m.name, m.index));
    restoreDeviceChoice("cqt_monitor_mic", sel);
  } catch (e) { toast("加载麦克风失败：" + e.message); }
}

function playPcm16(bytes) {
  if (!bytes || !bytes.length || !audioCtx) return;
  const int16 = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  if (!int16.length) return;

  // 电平打点：连续约 2 秒都接近静音，就提示一次（帮助定位"没声音"是谁的问题）
  let sum = 0;
  for (let i = 0; i < int16.length; i += 16) sum += Math.abs(int16[i]);
  const rms = sum / Math.max(1, Math.ceil(int16.length / 16));
  if (rms < 30) {
    pcmLowCount++;
    if (pcmLowCount === 60) toast("没收到麦克风声音：请检查电脑上的麦克风设备和系统权限");
  } else {
    pcmLowCount = 0;
  }

  pcmChunks.push(int16);
  pcmTotal += int16.length;
  if (pcmTotal < 4096) return;
  const all = new Int16Array(pcmTotal);
  let off = 0;
  for (const c of pcmChunks) { all.set(c, off); off += c.length; }
  pcmChunks = [];
  pcmTotal = 0;
  if (audioCtx && nextTime < audioCtx.currentTime - 0.5) {
    nextTime = audioCtx.currentTime + 0.03;
  }
  // 只累积不追：落后太多就直接跳到"现在"，避免越积越多（听感上就是几十秒延迟）
  if (audioCtx && nextTime > audioCtx.currentTime + 0.35) {
    nextTime = audioCtx.currentTime + 0.05;
  }
  const f32 = new Float32Array(all.length);
  for (let k = 0; k < all.length; k++) f32[k] = all[k] / 32768;
  const buf = audioCtx.createBuffer(1, f32.length, playRate);
  buf.copyToChannel(f32, 0);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  const t = Math.max(nextTime, audioCtx.currentTime + 0.03);
  src.start(t);
  nextTime = t + buf.duration;
}

function camAck() {
  if (camWs && camWs.readyState === 1) {
    try { camWs.send("1"); } catch (e) { /* 忽略 */ }
  }
}

async function startMonitor() {
  const cam = $("cam-select").value;
  const mic = $("mic-select").value;
  if (cam === "") { toast("请先选择摄像头"); return; }
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
    nextTime = audioCtx.currentTime + 0.08;

    // 摄像头 WebSocket
    const cUrl = await Auth.urlWithToken("/ws/monitor/camera?cam=" + encodeURIComponent(cam));
    camWs = openWs(cUrl);
    camWs.binaryType = "arraybuffer";
    camWs.onmessage = (e) => {
      // 服务端按“回执”节流：解码忙时立刻回执并丢弃这一帧，避免画面越拖越卡
      if (videoBusy) { camAck(); return; }
      videoBusy = true;
      const blob = new Blob([e.data], { type: "image/jpeg" });
      const canvas = $("video");
      const cctx = canvas.getContext("2d");
      if (window.createImageBitmap) {
        createImageBitmap(blob).then((bmp) => {
          canvas.width = bmp.width;
          canvas.height = bmp.height;
          cctx.drawImage(bmp, 0, 0);
          bmp.close();
          canvas.classList.remove("hidden");
          $("video-hint").classList.add("hidden");
          videoBusy = false;
          camAck();
        }).catch(() => { videoBusy = false; camAck(); });
      } else {
        const u = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          canvas.width = img.width;
          canvas.height = img.height;
          cctx.drawImage(img, 0, 0);
          URL.revokeObjectURL(u);
          canvas.classList.remove("hidden");
          $("video-hint").classList.add("hidden");
          videoBusy = false;
          camAck();
        };
        img.onerror = () => { URL.revokeObjectURL(u); videoBusy = false; camAck(); };
        img.src = u;
      }
    };
    camWs.onerror = () => { toast("摄像头连接失败"); };
    camWs.onclose = () => {};

    // 麦克风 WebSocket
    const mUrl = await Auth.urlWithToken("/ws/monitor/audio?mic=" + encodeURIComponent(mic));
    micWs = openWs(mUrl);
    micWs.binaryType = "arraybuffer";
    micWs.onmessage = (e) => {
      if (typeof e.data === "string") {
        // 服务端握手：告诉我们真实采样率
        try {
          const h = JSON.parse(e.data);
          if (h && h.rate) {
            playRate = h.rate | 0;
            if (audioCtx && audioCtx.sampleRate && playRate !== audioCtx.sampleRate) {
              console.log("[监听] 服务端采样率", playRate, "音频上下文", audioCtx.sampleRate);
            }
          }
        } catch (err) { /* 忽略非 JSON */ }
        return;
      }
      try {
        if (e.data instanceof Blob) {
          e.data.arrayBuffer().then((buf) => playPcm16(new Uint8Array(buf)))
            .catch((err) => console.warn("音频 Blob 解析失败", err));
        } else {
          playPcm16(new Uint8Array(e.data));
        }
      } catch (err) {
        console.warn("音频播放异常", err);
      }
    };
    micWs.onerror = () => { toast("麦克风连接失败"); };

    monitoring = true;
    $("btn-start").classList.add("hidden");
    $("btn-stop").classList.remove("hidden");
  } catch (e) {
    stopMonitor();
    if (handleAuthError(e)) toast("启动失败：" + e.message);
  }
}

function stopMonitor() {
  monitoring = false;
  videoBusy = false;
  pcmChunks = [];
  pcmTotal = 0;
  if (camWs) { try { camWs.close(); } catch (e) {} camWs = null; }
  if (micWs) { try { micWs.close(); } catch (e) {} micWs = null; }
  if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
  const canvas = $("video");
  if (canvas) {
    canvas.width = 0;
    canvas.height = 0;
    canvas.classList.add("hidden");
  }
  $("video-hint").textContent = "监控已停止";
  $("video-hint").classList.remove("hidden");
  $("btn-start").classList.remove("hidden");
  $("btn-stop").classList.add("hidden");
}

/* ---------- 对讲 ---------- */
async function startTalk() {
  try {
    talkStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 48000, echoCancellation: false, noiseSuppression: false },
    });
  } catch (e) {
    toast("无法打开浏览器麦克风，请检查权限");
    return;
  }
  const url = await Auth.urlWithToken("/ws/monitor/intercom").catch((e) => {
    if (handleAuthError(e)) throw e;
    throw e;
  });
  talkWs = openWs(url);
  talkWs.binaryType = "arraybuffer";
  talkWs.onopen = () => {
    talkCtx = new (window.AudioContext || window.webkitAudioContext)();
    talkCtx.resume().catch(() => {});
    // 第一条文本帧告诉服务端本机采样率（服务端据此决定播放采样率）
    try {
      talkWs.send(JSON.stringify({
        type: "hello",
        rate: talkCtx.sampleRate || 48000,
        format: "s16le",
      }));
    } catch (e) { /* 忽略 */ }
    const srcNode = talkCtx.createMediaStreamSource(talkStream);
    talkProc = talkCtx.createScriptProcessor(4096, 1, 1);
    talkChunks = [];
    talkProc.onaudioprocess = (e) => {
      const d = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(d.length);
      for (let i = 0; i < d.length; i++) {
        let s = Math.max(-1, Math.min(1, d[i]));
        i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      talkChunks.push(i16);
      let total = 0;
      for (const c of talkChunks) total += c.length;
      if (total >= 1920) flushTalk();   // 40ms 一片，降低上行延迟
    };
    srcNode.connect(talkProc);
    const gain = talkCtx.createGain();
    gain.gain.value = 0; // 本地静音，避免啸叫
    talkProc.connect(gain);
    gain.connect(talkCtx.destination);
    talking = true;
    $("btn-talk").textContent = "🔴 停止对讲";
    $("talk-status").textContent = "对讲中…";
  };
  talkWs.onerror = () => { toast("对讲连接失败"); stopTalk(); };
  talkWs.onclose = () => { if (talking) stopTalk(); };
}

function flushTalk() {
  if (!talkWs || talkWs.readyState !== 1) { talkChunks = []; return; }
  const all = new Int16Array(talkChunks.reduce((n, c) => n + c.length, 0));
  let off = 0;
  for (const c of talkChunks) { all.set(c, off); off += c.length; }
  talkChunks = [];
  try { talkWs.send(all.buffer); } catch (e) { /* 忽略 */ }
}

function stopTalk() {
  talking = false;
  if (talkProc) { try { talkProc.disconnect(); } catch (e) {} talkProc = null; }
  if (talkStream) { try { talkStream.getTracks().forEach((t) => t.stop()); } catch (e) {} talkStream = null; }
  if (talkCtx) { try { talkCtx.close(); } catch (e) {} talkCtx = null; }
  if (talkWs) { try { talkWs.close(); } catch (e) {} talkWs = null; }
  $("btn-talk").textContent = "🎤 开始对讲";
  $("talk-status").textContent = "";
}

/* ---------- 事件 ---------- */
$("cam-select").addEventListener("change", () => saveDeviceChoice("cqt_monitor_cam", $("cam-select")));
$("mic-select").addEventListener("change", () => saveDeviceChoice("cqt_monitor_mic", $("mic-select")));
$("btn-start").addEventListener("click", startMonitor);
$("btn-stop").addEventListener("click", stopMonitor);
$("btn-talk").addEventListener("click", () => { talking ? stopTalk() : startTalk(); });

window.addEventListener("beforeunload", () => { stopMonitor(); stopTalk(); });

loadDevices();
