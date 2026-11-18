/* 首次设置向导逻辑：分步、许可证阅读门禁、二维码、提交 */
"use strict";
const $ = (id) => document.getElementById(id);
const scenes = ["forest", "desert", "ocean", "snow", "starry", "grass", "sakura"];
const decos = {
  forest: ["🌲", "🌳", "🍄", "🦋", "🌿"],
  desert: ["🌵", "🐫", "🏜", "☀️", "🦂"],
  ocean: ["🐳", "🐠", "🐚", "⛵", "🌊"],
  snow: ["❄️", "⛄", "🏔", "🌨", "🐧"],
  starry: ["⭐", "🌙", "✨", "🪐", "☄️"],
  grass: ["🐑", "🌾", "🌻", "🦗", "🌱"],
  sakura: ["🌸", "🌷", "🦩", "🍃", "🌺"],
};
const totalSteps = document.querySelectorAll(".step").length;
const state = {
  step: 0,
  lic: { zh: { bottom: false, t0: 0 }, en: { bottom: false, t0: 0 } },
  lang: "zh",
  host: "",
  roster: [],
  pairing: null,
  licTimer: null,
};

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add("hidden"), 2600);
}

async function api(url, opt = {}) {
  const o = Object.assign({}, opt);
  if (typeof o.body === "string") {
    o.headers = Object.assign({ "Content-Type": "application/json" }, o.headers || {});
  }
  const res = await fetch(url, o);
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

/* ---------- 背景 & 步进 ---------- */
function buildDots() {
  $("dots").innerHTML = "";
  for (let i = 0; i < totalSteps; i++) {
    const d = document.createElement("i");
    $("dots").appendChild(d);
  }
}

function paintDeco(scene) {
  const box = $("deco");
  box.innerHTML = "";
  const list = decos[scene] || [];
  const spots = [[8, 62], [22, 78], [40, 88], [62, 74], [80, 84], [90, 60]];
  list.forEach((ch, i) => {
    const s = document.createElement("span");
    const p = spots[i % spots.length];
    s.textContent = ch;
    s.style.left = p[0] + "%";
    s.style.top = p[1] + "%";
    s.style.animationDelay = (i * 0.7) + "s";
    box.appendChild(s);
  });
}

function go(step) {
  if (step < 0 || step >= totalSteps) return;
  state.step = step;
  document.querySelectorAll(".step").forEach((el, i) => {
    el.classList.toggle("active", i === step);
  });
  const scene = scenes[step % scenes.length];
  document.body.dataset.scene = scene;
  paintDeco(scene);
  const dots = $("dots").children;
  for (let i = 0; i < dots.length; i++) {
    dots[i].className = i === step ? "on" : (i < step ? "done" : "");
  }
  $("step-label").textContent = "第 " + (step + 1) + " / " + totalSteps + " 步";
  $("btn-prev").classList.toggle("hidden", step === 0);
  $("btn-next").classList.toggle("hidden", step === totalSteps - 1);
  if (step === 0) fireworks(1600);
  if (step === totalSteps - 1) { buildSummary(); confetti(); }
  if (step === 2) $("host-name").focus();
  if (step === 5) { if (state.pairToken) drawQR(); else makePairQR(); }
  refreshGate();
}

function canNext() {
  const s = state.step;
  if (s === 1) {
    const cur = state.lic[state.lang];
    return cur.bottom && (Date.now() - cur.t0 >= 10000) && $("lic-agree").checked;
  }
  if (s === 2) {
    const v = ($("host-name").value || "").trim();
    return /^[a-z0-9]([a-z0-9-]{0,22})$/.test(v);
  }
  return true;
}

function refreshGate() {
  $("btn-next").disabled = !canNext();
}

/* ---------- 许可证 ---------- */
async function loadLicense() {
  try {
    const d = await api("/api/wizard/license");
    $("lic-box").dataset.zh = d.zh || "（缺少中文译文文件）";
    $("lic-box").dataset.en = d.en || "（缺少英文 LICENSE 文件）";
    showLang("zh");
  } catch (e) {
    $("lic-box").textContent = "许可证读取失败：" + e.message;
  }
}

function showLang(lang) {
  state.lang = lang;
  document.querySelectorAll(".lic-tabs .tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.lang === lang));
  const box = $("lic-box");
  box.textContent = box.dataset[lang] || "加载中…";
  box.scrollTop = 0;
  $("lic-note").textContent = lang === "zh"
    ? "中文译文仅供参考，具法律效力的是英文原文（可切到 English 对照）"
    : "GNU 官方英文原文（具法律效力）";
  if (!state.lic[lang].t0) state.lic[lang].t0 = Date.now();
  state.lic[lang].bottom = false;
  $("lic-agree").checked = false;
  $("lic-agree").disabled = true;
  refreshGate();
}

function onLicScroll() {
  const box = $("lic-box");
  const near = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  if (near) {
    const cur = state.lic[state.lang];
    if (!cur.bottom) {
      cur.bottom = true;
      $("lic-agree").disabled = false;
    }
  }
  tickLic();
}

function tickLic() {
  const cur = state.lic[state.lang];
  const el = $("lic-progress");
  if (!cur.bottom) {
    el.className = "lic-progress";
    el.textContent = "请向下滚动阅读到底部";
    return;
  }
  const left = Math.max(0, 10000 - (Date.now() - cur.t0));
  if (left > 0) {
    el.className = "lic-progress";
    el.textContent = "已读到底部，还需停留 " + Math.ceil(left / 1000) + " 秒（认真读一下哦）";
    refreshGate();
    return;
  }
  el.className = "lic-progress ok";
  el.textContent = "✔ 已读完，可以勾选同意后继续";
  refreshGate();
}

/* ---------- 主机名 ---------- */
function syncHost() {
  const v = ($("host-name").value || "").trim().toLowerCase();
  $("host-name").value = v;
  const ok = /^[a-z0-9]([a-z0-9-]{0,22})$/.test(v);
  $("host-preview").textContent = ok ? ("http://" + v + ".local:" + (window.__port || 8000)) : "（名称无效）";
  state.host = ok ? v : "";
  state.hostChanged = !!state.host && !!window.__defHost && state.host !== window.__defHost;
  refreshGate();
}

/* ---------- 名条 ---------- */
function parseRoster(text) {
  const out = [];
  String(text || "").split(/\r?\n/).forEach((line) => {
    const t = line.trim();
    if (!t) return;
    const parts = t.split(/[,，\t;；]+|\s{1,}/).filter((x) => x !== "");
    if (!parts.length) return;
    if (parts.length === 1) out.push({ number: "", name: parts[0] });
    else out.push({ number: parts[0], name: parts.slice(1).join(" ") });
  });
  return out;
}

function syncRoster() {
  state.roster = parseRoster($("roster-text").value);
  $("roster-count").textContent = "当前 " + state.roster.length + " 条";
  refreshGate();
}

/* ---------- 二维码 ---------- */
function phoneUrl() {
  // 手机激活/监控需要 https（浏览器只在安全上下文里提供 crypto.subtle）
  let host = state.host || window.__defHost || "127.0.0.1";
  if (host.indexOf(".") < 0) host += ".local";
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ":" + port;
}

function drawQR() {
  const base = phoneUrl();                    // https://host.local:9443
  const url = state.pairToken ? (base.replace(/\/+$/, "") + "/activate?t=" + encodeURIComponent(state.pairToken)) : base;
  $("qr-url").textContent = state.pairToken
    ? "扫码即自动激活（30 分钟内有效）"
    : url;
  const hint = $("qr-hint");
  if (hint) {
    if (window.__secure) {
      hint.textContent = state.hostChanged
        ? "刚改过设备名：手机第一次打开会提示「证书不安全」，点「高级 → 继续访问」即可；点完成页的「🔄 现在重启」后，新名字的证书才会自动生效。"
        : "手机上首次打开会提示「证书不安全」，点「高级 → 继续访问」即可；激活必须用 https（http 下浏览器不给加密能力）。";
    } else {
      hint.textContent = "当前没有启用 HTTPS 安全端口，手机只能叫号、不能激活；请先到「设置」里勾选「启用 HTTPS 安全端口」。";
    }
  }
  const cv = $("qr");
  if (typeof qrcode !== "function") return;
  try {
    const qr = qrcode(0, "M");
    qr.addData(url);
    qr.make();
    const ctx = cv.getContext("2d");
    const n = qr.getModuleCount();
    const cell = Math.floor(200 / n);
    const size = cell * n;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, 200, 200);
    ctx.fillStyle = "#0f172a";
    const off = Math.floor((200 - size) / 2);
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (qr.isDark(r, c)) ctx.fillRect(off + c * cell, off + r * cell, cell, cell);
      }
    }
  } catch (e) { /* 忽略：二维码失败不影响流程 */ }
}

/* ---------- 扫码配对（扫一下就自动激活） ---------- */
async function makePairQR() {
  const box = $("pw-value");
  try {
    const d = await api("/api/auth/qr_create", {
      method: "POST",
      body: JSON.stringify({ minutes: 30, valid_days: 365, name: "手机扫码配对" }),
    });
    state.pairToken = d.token;
    window.__pairToken = d.token;   // 方便调试/自动化测试读取
    state.pairing = d.code;      // 备用配对码
    state.pairExpires = Date.now() + (d.expires_in || 1800) * 1000;
    if (box) box.textContent = d.code;
    $("pw-show").classList.remove("hidden");
    drawQR();
    toast("二维码已生成，30 分钟内有效");
  } catch (e) {
    if (box) box.textContent = "生成失败";
    toast("生成二维码失败：" + e.message);
  }
}

/* ---------- 完成 ---------- */
function buildSummary() {
  const host = state.host || "127.0.0.1";
  const port = window.__port || 8000;
  const httpsUrl = window.__secure
    ? "https://" + (host.indexOf(".") < 0 ? host + ".local" : host) + ":" + (window.__securePort || 9443)
    : "";
  const items = [
    "手机激活 / 监控地址（https）：<b>" + (httpsUrl || "未启用 HTTPS，请到设置里开启") + "</b>",
    "手机叫号地址：<b>http://" + host + ".local:" + port + "</b>",
    "学生名条：<b>" + state.roster.length + " 条</b>" + (state.roster.length ? "" : "（稍后可添加）"),
    "开机自启：<b>" + ($("opt-autostart").checked ? "已开启" : "未开启") + "</b>",
    "更新：<b>" + $("opt-channel").selectedOptions[0].textContent + " / " +
      $("opt-policy").selectedOptions[0].textContent + "</b>",
    "手机扫码激活：<b>" + (state.pairToken
      ? "二维码已生成（30 分钟内有效，扫码即自动激活）"
      : "未生成，可稍后在「🔐 密码管理」里创建") + "</b>",
    "备用配对码：<b>" + (state.pairing || "-") + "</b>",
  ];
  if (state.hostChanged) {
    items.push("设备名：<b>" + host + ".local</b> —— 手机上 http 立刻可用；"
      + "https（监控 / 文件 / 更新）要点下面的「🔄 现在重启」才会换用新名字的证书");
  }
  $("summary").innerHTML = items.map((t) => "<li>" + t + "</li>").join("");
}

async function finish() {
  const btn = $("btn-start");
  btn.disabled = true;
  btn.textContent = "正在保存…";
  try {
    const d = await api("/api/wizard/finish", {
      method: "POST",
      body: JSON.stringify({
        mdns_hostname: state.host,
        mdns_enabled: true,
        autostart: $("opt-autostart").checked,
        update_channel: $("opt-channel").value,
        update_policy: $("opt-policy").value,
        roster: state.roster,
      }),
    });
    // 改过设备名：mDNS 名字立刻生效，但 https 证书要重启才换上
    if (d && d.restart_needed) {
      btn.disabled = false;
      btn.textContent = "✅ 先这样，进入使用";
      const tip = $("restart-tip");
      if (tip) tip.classList.remove("hidden");
      const rb = $("btn-restart");
      if (rb) rb.onclick = doRestart;
      toast("设备名已保存：http 立刻可用，https 需要重启一次");
      return;
    }
    btn.textContent = "✅ 正在进入…";
    setTimeout(() => { location.href = "/"; }, 500);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "🚀 立即使用";
    toast("保存失败：" + e.message);
  }
}

async function doRestart() {
  if (!confirm("现在重启「班级叫号器」吗？\n\n网页会断开几秒，重启后刷新一下就好。")) return;
  const rb = $("btn-restart");
  if (rb) { rb.disabled = true; rb.textContent = "正在重启…"; }
  try {
    await api("/api/restart", { method: "POST", body: "{}" });
    toast("正在重启，5 秒后自动刷新…");
    setTimeout(() => { location.href = "/"; }, 6000);
  } catch (e) {
    if (rb) { rb.disabled = false; rb.textContent = "🔄 现在重启"; }
    toast("重启失败：" + e.message + "（可以手动关掉软件再打开）");
  }
}

/* ---------- 烟花 / 礼炮 ---------- */
const fx = $("fx");
const ctx = fx.getContext("2d");
let parts = [];
let rafId = null;

function resizeFx() {
  fx.width = window.innerWidth;
  fx.height = window.innerHeight;
}
window.addEventListener("resize", resizeFx);
resizeFx();

function burst(x, y, n, palette) {
  for (let i = 0; i < n; i++) {
    const a = Math.random() * Math.PI * 2;
    const sp = 1.6 + Math.random() * 4.2;
    parts.push({
      x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 1.2,
      life: 60 + Math.random() * 40, age: 0,
      color: palette[Math.floor(Math.random() * palette.length)],
      size: 1.6 + Math.random() * 2.4,
    });
  }
  if (!rafId) loopFx();
}

function fireworks(times) {
  const palettes = [["#f43f5e", "#facc15", "#22d3ee", "#a78bfa"], ["#34d399", "#60a5fa", "#fbbf24", "#f472b6"]];
  let n = 0;
  const timer = setInterval(() => {
    // 只在卡片上方的天空带和下方地面带炸开，别挡住正文
    const lower = n % 3 === 2;
    const x = fx.width * (0.15 + Math.random() * 0.7);
    const y = lower ? fx.height * (0.80 + Math.random() * 0.12)
                    : fx.height * (0.06 + Math.random() * 0.12);
    burst(x, y, 46, palettes[n % 2]);
    if (++n >= 5) clearInterval(timer);
  }, 300);
}

function confetti() {
  const palettes = [["#f472b6", "#facc15", "#34d399", "#60a5fa", "#c084fc"]];
  // 彩纸从顶部飘落，覆盖整屏
  for (let i = 0; i < 5; i++) {
    burst(fx.width * (0.15 + i * 0.18), fx.height * 0.04, 40, palettes[0]);
  }
}

function loopFx() {
  ctx.clearRect(0, 0, fx.width, fx.height);
  parts = parts.filter((p) => p.age < p.life);
  parts.forEach((p) => {
    p.age += 1;
    p.x += p.vx;
    p.y += p.vy;
    p.vy += 0.055;
    p.vx *= 0.992;
    const alpha = Math.max(0, 1 - p.age / p.life);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = p.color;
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;
  if (parts.length) {
    rafId = requestAnimationFrame(loopFx);
  } else {
    cancelAnimationFrame(rafId);
    rafId = null;
    ctx.clearRect(0, 0, fx.width, fx.height);
  }
}

/* ---------- 事件 ---------- */
$("btn-next").addEventListener("click", () => { if (canNext()) go(state.step + 1); });
$("btn-prev").addEventListener("click", () => go(state.step - 1));
$("lic-box").addEventListener("scroll", onLicScroll);
document.querySelectorAll(".lic-tabs .tab").forEach((b) =>
  b.addEventListener("click", () => showLang(b.dataset.lang)));
$("lic-agree").addEventListener("change", () => { tickLic(); refreshGate(); });
$("host-name").addEventListener("input", syncHost);
$("roster-text").addEventListener("input", syncRoster);
$("btn-pick-file").addEventListener("click", () => $("roster-file").click());
$("roster-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fr = new FileReader();
  fr.onload = () => {
    const text = String(fr.result || "").replace(/^学号.*\n/, "");
    $("roster-text").value = text.trim();
    syncRoster();
    toast("已读入 " + state.roster.length + " 条");
  };
  fr.readAsText(f, "utf-8");
});
$("btn-sample").addEventListener("click", () => {
  $("roster-text").value = "1,张子墨\n2,王欣然\n3,李嘉怡\n4,赵晨曦\n5,陈思远\n6,刘雨桐";
  syncRoster();
});
$("btn-genpw").addEventListener("click", makePairQR);
$("btn-copy").addEventListener("click", async () => {
  const t = $("pw-value").textContent;
  try {
    await navigator.clipboard.writeText(t);
    toast("已复制");
  } catch (e) { toast("请手动选中复制：" + t); }
});
$("btn-start").addEventListener("click", finish);

/* ---------- 启动 ---------- */
(async function init() {
  buildDots();
  try {
    const st = await api("/api/wizard/state");
    window.__port = st.port || 8000;
    window.__secure = !!st.secure_enabled;
    window.__securePort = st.secure_port || 9443;
    window.__defHost = st.hostname || "";
    if (st.done) { location.href = "/"; return; }
    $("host-name").value = st.hostname || "";
    $("opt-autostart").checked = !!st.autostart;
    if (st.update_channel) $("opt-channel").value = st.update_channel;
    if (st.update_policy) $("opt-policy").value = st.update_policy;
    if (st.roster_count > 0) {
      $("roster-hint").textContent =
        "检测到已有 " + st.roster_count + " 条名条：这里留空就保持原来的名条不变。";
    }
  } catch (e) { /* 忽略，用默认值 */ }
  syncHost();
  await loadLicense();
  go(0);
  setInterval(() => { if (state.step === 1) tickLic(); }, 500);
})();
