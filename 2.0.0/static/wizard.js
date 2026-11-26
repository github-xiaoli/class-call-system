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
  hostChanged: false,   // 输入框里的名字与初始值不同
  hostRenamed: false,   // 本次向导里真的改过名字（保存过）
  restartNeeded: false, // 改过名字 → https 需重启一次
  roster: [],
  pairing: null,
  licTimer: null,
  restoring: false,   // 正在恢复上次进度，不要触发保存
  saveTimer: null,
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

function collectFields() {
  const f = {};
  const hostEl = $("host-name");
  if (hostEl) f.mdns_hostname = (hostEl.value || "").trim();
  const asEl = $("opt-autostart");
  if (asEl) f.autostart = !!asEl.checked;
  const chEl = $("opt-channel");
  if (chEl) f.update_channel = chEl.value;
  const polEl = $("opt-policy");
  if (polEl) f.update_policy = polEl.value;
  if (state.roster && state.roster.length) f.roster = state.roster;
  const ph = $("wiz-phrase");
  if (ph) f.phrase = ph.value;
  const pc = $("wiz-play-count");
  if (pc) f.play_count = Number(pc.value) || 1;
  const vo = $("wiz-volume");
  if (vo) f.volume = Number(vo.value) || 0;
  const ps = $("wiz-prompt-sound");
  if (ps) f.prompt_sound = ps.value;
  const pe = $("wiz-prompt-enabled");
  if (pe) f.prompt_enabled = !!pe.checked;
  const te = $("wiz-tts-engine");
  if (te) f.tts_engine = te.value;
  return f;
}

function saveStep(step) {
  if (state.restoring) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => {
    api("/api/wizard/step", {
      method: "POST",
      body: JSON.stringify({ step: step, fields: collectFields(),
                             license_agreed: !!($("lic-agree") || {}).checked }),
    }).catch(() => {});
  }, 120);
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
  if (step === 1) loadLicense();   // 进到许可证这一屏必定加载一次
  if (step === 2) $("host-name").focus();
  if (step === 5) { if (state.pairToken) drawQR(); else makePairQR(); }
  if (step === 5) {
    const mu = $("pair-manual-url");
    if (mu) {
      const h = state.host || window.__defHost || "127.0.0.1";
      const pt = window.__port || 8000;
      mu.textContent = "http://" + h + ".local:" + pt + "/pair";
    }
  }
  refreshGate();
  saveStep(step);
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
  const box = $("lic-box");
  const put = (zh, en, note) => {
    box.dataset.zh = zh;
    box.dataset.en = en;
    box.textContent = ($("lic-tabs") && document.querySelector(".lic-tabs .tab.active") &&
                       document.querySelector(".lic-tabs .tab.active").dataset.lang === "en") ? en : zh;
    box.scrollTop = 0;
    if (note && $("lic-note")) $("lic-note").textContent = note;
    // 加载完就把"划到底 10 秒"的计时重置，让用户从头读
    try {
      if (state && state.lic) {
        state.lic.zh = { t0: 0, bottom: false };
        state.lic.en = { t0: 0, bottom: false };
      }
    } catch (e) {}
    const cb = $("lic-agree");
    if (cb) { cb.checked = false; cb.disabled = true; }
    refreshGate();
  };

  try {
    const d = await Promise.race([
      api("/api/wizard/license"),
      new Promise((_, rej) => setTimeout(() => rej(new Error("超过 10 秒未返回")), 10000)),
    ]);
    const zh = (d && d.zh) || "";
    const en = (d && d.en) || "";
    if (!zh && !en) throw new Error("服务器上没有找到许可证文件");
    put(zh || en, en || zh, "");
  } catch (e) {
    const txt = "本地许可证文件读取失败（" + (e.message || "未知原因") + "）。\n\n" +
      "本软件以 GNU 通用公共许可证第 3 版（GPL-3.0-or-later）发布，全文可在此查看／下载：\n" +
      "  英文原文（具法律效力）: https://www.gnu.org/licenses/gpl-3.0.txt\n" +
      "  中文参考（非官方）    : https://www.gnu.org/licenses/gpl-3.0.zh-cn.html\n\n" +
      "安装目录里也带了一份 LICENSE 文件，可直接打开。\n\n" +
      "阅读后可勾选下方「我已阅读并同意上述许可证条款」继续。";
    put(txt, txt, "已切换到离线兜底说明");
    const cb = $("lic-agree");
    if (cb) cb.disabled = false;
    refreshGate();
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

/* 离开设备名那一步时，立刻把名字存下来并重新注册 mDNS（不等最后一步） */
async function saveHostnameNow() {
  if (!state.host) return false;
  try {
    const d = await api("/api/wizard/hostname", {
      method: "POST",
      body: JSON.stringify({ mdns_hostname: state.host }),
    });
    if (d && d.changed) {
      // 本次向导里确实改过名字：记住这个事实（名字保存后两者就相等了，不能靠比较判断）
      state.hostRenamed = true;
      state.hostChanged = true;
      state.restartNeeded = true;
      window.__defHost = d.hostname || window.__defHost;
      toast("设备名已保存，mDNS 已切换为 " + d.hostname + ".local");
    }
    return true;
  } catch (e) {
    toast("设备名保存失败：" + e.message);
    return false;
  }
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
/* 手机访问的主机名：优先局域网 IP（任何手机都能打开）。
   .local 靠设备自己解析 mDNS：苹果设备/电脑/较新安卓可用，部分华为鸿蒙等机型不可用，
   所以主地址用 IP，设备名只作为备选显示。 */
function phoneHost() {
  const ips = window.__lanIps || [];
  const ip = ips.find((v) => !v.startsWith("127.") && !v.startsWith("169.254."));
  if (ip) return ip;
  let host = state.host || window.__defHost || "127.0.0.1";
  if (host.indexOf(".") < 0) host += ".local";
  return host;
}

function phoneUrl() {
  // 手机激活/监控需要 https（浏览器只在安全上下文里提供 crypto.subtle）
  const host = phoneHost();
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ":" + port;
}

/* 用设备名访问的地址（苹果 / 电脑可用） */
function phoneNameUrl() {
  const host = state.host || window.__defHost || "";
  if (!host || host.indexOf(".") >= 0) return "";
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ".local:" + port;
}

function drawQR() {
  const base = phoneUrl();                    // https://host.local:9443
  const url = state.pairToken ? (base.replace(/\/+$/, "") + "/activate?t=" + encodeURIComponent(state.pairToken)) : base;
  const nameUrl = phoneNameUrl();
  $("qr-url").textContent = (state.pairToken
    ? "扫码即自动激活（30 分钟内有效）：" + url
    : url) + (nameUrl ? "\n　iPhone/电脑也可用 " + nameUrl : "");
  const hint = $("qr-hint");
  if (hint) {
    if (window.__secure) {
      hint.textContent = (state.hostRenamed || state.hostChanged)
        ? "刚改过设备名：手机第一次打开会提示「证书不安全」，点「高级 → 继续访问」即可；点完成页的「🔄 现在重启」后，新名字的证书才会自动生效。"
        : "手机上首次打开会提示「证书不安全」，点「高级 → 继续访问」即可；激活必须用 https（http 下浏览器不给加密能力）。";
    } else {
      hint.textContent = "当前没有启用 HTTPS 安全端口，手机只能叫号、不能激活；请先到「设置」里勾选「启用 HTTPS 安全端口」。";
    }
  }
  const cv = $("qr");
  if (!cv) return;
  cv.dataset.content = url;          // 方便自查二维码指向
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
let pairBusy = false;
async function makePairQR() {
  const box = $("pw-value");
  const btn = $("btn-genpw");
  // 连点保护：正在生成时直接忽略，避免每次点击都新建一张密码
  if (pairBusy) return;
  pairBusy = true;
  const btnText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "生成中…";
  }
  try {
    const d = await api("/api/auth/qr_create", {
      method: "POST",
      body: JSON.stringify({ minutes: 30, valid_days: 365,
        name: (($("pair-name") || {}).value || "").trim() || "手机扫码配对",
        note: (($("pair-note") || {}).value || "").trim() }),
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
  } finally {
    pairBusy = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = btnText || "🔄 重新生成二维码";
    }
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
  if (state.hostRenamed || state.hostChanged) {
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
$("btn-next").addEventListener("click", async () => {
  if (!canNext()) return;
  // 第 3 步（设备名）：先落库并重新注册 mDNS，再进入下一步
  if (state.step === 2) {
    const btn = $("btn-next");
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "保存中…";
    await saveHostnameNow();
    btn.textContent = old;
    btn.disabled = false;
  }
  go(state.step + 1);
});
$("btn-prev").addEventListener("click", () => go(state.step - 1));
$("lic-box").addEventListener("scroll", onLicScroll);
document.querySelectorAll(".lic-tabs .tab").forEach((b) =>
  b.addEventListener("click", () => showLang(b.dataset.lang)));
$("lic-agree").addEventListener("change", () => { tickLic(); refreshGate(); });
// 任何一项改动都即时保存（异步防抖，不会打断输入）
function autosaveField() { saveStep(state.step); }
$("host-name").addEventListener("input", syncHost);
$("host-name").addEventListener("change", autosaveField);
function previewWizPhrase() {
  const el = $("wiz-phrase-preview");
  const src = $("wiz-phrase");
  if (!el || !src) return;
  el.textContent = String(src.value || "")
    .replace(/%number%/g, "12").replace(/%name%/g, "张子墨")
    .replace(/%note%/g, "数学课代表").replace(/%teacher%/g, "王老师") || "（未设置叫号语）";
}
$("wiz-phrase").addEventListener("input", () => { previewWizPhrase(); autosaveField(); });
["wiz-play-count", "wiz-volume", "wiz-prompt-sound", "wiz-prompt-enabled", "wiz-tts-engine"]
  .forEach((id) => { const el = $(id); if (el) { el.addEventListener("change", autosaveField); el.addEventListener("input", autosaveField); } });

["opt-autostart", "opt-channel", "opt-policy"].forEach((id) => {
  const el = $(id);
  if (el) {
    el.addEventListener("change", autosaveField);
    el.addEventListener("input", autosaveField);
  }
});
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
    window.__lanIps = st.local_ips || [];
    if (st.done) { location.href = "/"; return; }
    $("host-name").value = st.hostname || "";
    $("opt-autostart").checked = !!st.autostart;
    if (st.update_channel) $("opt-channel").value = st.update_channel;
    if (st.update_policy) $("opt-policy").value = st.update_policy;
    if (st.roster_count > 0) {
      $("roster-hint").textContent =
        "检测到已有 " + st.roster_count + " 条名条：这里留空就保持原来的名条不变。";
    }
    state.resumeStep = Number(st.step || 0) || 0;
    state.resumeAgreed = !!st.license_agreed;
    if ($("wiz-phrase")) $("wiz-phrase").value = st.phrase || "";
    if ($("wiz-play-count")) $("wiz-play-count").value = st.play_count || 1;
    if ($("wiz-volume")) $("wiz-volume").value = (st.volume != null ? st.volume : 100);
    if ($("wiz-prompt-sound")) $("wiz-prompt-sound").value = st.prompt_sound || "beep";
    if ($("wiz-prompt-enabled")) $("wiz-prompt-enabled").checked = !!st.prompt_enabled;
    if ($("wiz-tts-engine")) $("wiz-tts-engine").value = st.tts_engine || "auto";
    previewWizPhrase();
  } catch (e) { /* 忽略，用默认值 */ }
  syncHost();
  await loadLicense();
  // 恢复"已同意许可证"（必须放在 loadLicense 之后，否则会被初始化重置）
  if (state.resumeAgreed) {
    const agree = $("lic-agree");
    if (agree) agree.checked = true;
    const cur = state.lic[state.lang];
    if (cur) { cur.bottom = true; cur.t0 = Date.now() - 11000; }
    if (typeof tickLic === "function") tickLic();
  }
  // 回到上次那一步（没同意许可证就停在许可证页）
  let resume = Number(state.resumeStep || 0);
  const agreed = ($("lic-agree") || {}).checked;
  if (!agreed && resume > 1) resume = 1;
  if (resume > totalSteps - 1) resume = totalSteps - 1;
  if (resume < 0) resume = 0;
  state.restoring = true;
  go(resume);
  state.restoring = false;
  if (resume > 0) toast("已从第 " + (resume + 1) + " 步继续");
  setInterval(() => { if (state.step === 1) tickLic(); }, 500);
})();
