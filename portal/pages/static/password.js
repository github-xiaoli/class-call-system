/* 密码管理（仅本机访问） */
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
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

async function loadPasswords() {
  try {
    const d = await api("/api/auth/passwords");
    renderList(d.passwords || []);
  } catch (e) {
    toast("加载失败：" + e.message);
  }
}

function statusOf(p) {
  if (p.bound) {
    return p.binding_expired ? '<span class="badge warn">已过期</span>'
      : '<span class="badge ok">已绑定</span>';
  }
  if (p.used) return '<span class="badge gray">已使用</span>';
  if (p.expired) return '<span class="badge warn">已超时</span>';
  return '<span class="badge ok">未激活</span>';
}

function renderList(list) {
  const box = $("pw-list");
  box.innerHTML = "";
  $("pw-empty").classList.toggle("hidden", list.length > 0);
  for (const p of list) {
    const div = document.createElement("div");
    div.className = "pw-item";
    div.innerHTML =
      '<div class="pw-main">' + statusOf(p) +
        '<span class="pw-id">' + esc(p.id.slice(0, 8)) + '</span>' +
        (p.name ? '<span class="pw-name">' + esc(p.name) + '</span>' : "") +
        (p.note ? '<span class="pw-time">备注：' + esc(p.note) + '</span>' : "") +
        '<span class="pw-time">创建 ' + esc(p.created_text) + '</span>' +
        '<span class="pw-time">激活时限至 ' + esc(p.activate_until_text) + '</span>' +
        (p.bound ? '<span class="pw-time">绑定有效期至 ' + esc(p.valid_until_text) + '</span>' : "") +
        (p.valid_days ? '<span class="pw-time">激活后有效期 ' + p.valid_days + ' 天</span>' :
          (p.valid_minutes ? '<span class="pw-time">激活后有效期 ' + p.valid_minutes + ' 分钟</span>' : "")) +
      '</div>' +
      '<button class="btn danger small" data-id="' + esc(p.id) + '">撤销</button>';
    div.querySelector("button").addEventListener("click", () => revoke(p.id));
    box.appendChild(div);
  }
}

async function createPassword() {
  const password = $("pw-content").value.trim();
  const name = $("pw-name").value.trim();
  const note = $("pw-note").value.trim();
  const activate_minutes = parseInt($("pw-activate-minutes").value, 10);
  const valid_days = parseInt($("pw-valid-days").value, 10);
  if (!password) { toast("请输入密码内容"); return; }
  try {
    await api("/api/auth/create", {
      method: "POST",
      body: JSON.stringify({ password, name, note, activate_minutes, valid_days }),
    });
    $("pw-content").value = "";
    $("pw-name").value = "";
    $("pw-note").value = "";
    toast("密码已创建");
    await loadPasswords();
  } catch (e) {
    toast("创建失败：" + e.message);
  }
}

async function revoke(id) {
  if (!confirm("确定撤销该密码吗？若已绑定设备，撤销后该设备将立即失效。")) return;
  try {
    const d = await api("/api/auth/revoke", { method: "POST", body: JSON.stringify({ id }) });
    toast(d.unbound ? "已撤销并解除设备绑定" : "已撤销");
    await loadPasswords();
  } catch (e) {
    toast("撤销失败：" + e.message);
  }
}

$("btn-random").addEventListener("click", () => {
  const n = 100000 + Math.floor(Math.random() * 900000);
  $("pw-content").value = String(n);
  toast("已生成随机密码：" + n);
});
$("btn-create").addEventListener("click", createPassword);
loadPasswords();


/* ---------- 扫码配对 ---------- */
let pairCode = "";

function lanIpOf(st) {
  st = st || {};
  const ips = st.local_ips || [];
  return ips.find((ip) => !ip.startsWith("127.") && !ip.startsWith("169.254.")) || "";
}

/* 二维码里放的地址：**优先用 IP**（所有手机、所有系统都能打开）。
   名字（xxx.local）靠设备自己解析 mDNS：iPhone / iPad / 电脑 / 较新安卓
   （Android 12+、LineageOS 等）可以，部分华为鸿蒙等机型不行 —— 实测
   荣耀30S(鸿蒙3.0) 报"找不到服务器"，Lenovo 平板(LineageOS 23 GSI) 正常。
   所以默认用 IP，名字只在页面文字里作备选。 */
function pairHostOf(st) {
  st = st || {};
  const ip = lanIpOf(st);
  if (ip) return ip;
  const host = st.mdns_hostname || "";
  if (!host) return "";
  return /^\d+\.\d+\.\d+\.\d+$/.test(host) ? host : host + ".local";
}

/* 备用：用设备名访问的地址（苹果/电脑可用）。 */
function pairNameUrlOf(st) {
  st = st || {};
  const host = st.mdns_hostname || "";
  if (!host || /^\d+\.\d+\.\d+\.\d+$/.test(host)) return "";
  const port = st.port || location.port || 8000;
  return "http://" + host + ".local:" + port + "/pair";
}

function pairPageUrlOf(st) {
  st = st || {};
  const host = pairHostOf(st);
  if (!host) return "（没有可用的局域网地址）";
  const port = st.port || location.port || 8000;
  let text = "http://" + host + ":" + port + "/pair";
  const nameUrl = pairNameUrlOf(st);
  if (nameUrl) text += "　（iPhone/电脑也可用 " + nameUrl + "）";
  return text;
}

function pairScanUrlOf(st) {
  st = st || {};
  const host = pairHostOf(st);
  if (!host) return "";
  if (st.secure_enabled) {
    return "https://" + host + ":" + (st.secure_port || 9443) + "/pair";
  }
  return "http://" + host + ":" + (st.port || location.port || 8000) + "/pair";
}

function drawPairQR(text) {
  const cv = document.getElementById("qr-canvas");
  if (!cv || typeof qrcode !== "function") return;
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const ctx = cv.getContext("2d");
  const n = qr.getModuleCount();
  const cell = Math.floor(200 / n);
  const size = cell * n;
  const off = Math.floor((200 - size) / 2);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, 200, 200);
  ctx.fillStyle = "#0f172a";
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) ctx.fillRect(off + c * cell, off + r * cell, cell, cell);
    }
  }
}

async function makePairQR() {
  const btn = document.getElementById("btn-qr-create");
  if (!btn || btn.disabled) return;
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const name = (document.getElementById("qr-name").value || "").trim();
    const note = (document.getElementById("qr-note").value || "").trim();
    const d = await api("/api/auth/qr_create", {
      method: "POST",
      body: JSON.stringify({ minutes: 30, valid_days: 365,
                             name: name || "手机扫码配对", note: note }),
    });
    pairCode = d.code;
    document.getElementById("qr-box").classList.remove("hidden");
    document.getElementById("qr-code").textContent = d.code;
    document.getElementById("qr-who").textContent =
      (d.name ? "名称：" + d.name : "") + (d.note ? "　备注：" + d.note : "");
    const st = await api("/api/status").catch(() => ({}));
    const scanBase = pairScanUrlOf(st);
    if (!scanBase) {
      toast("还没有设备名：请先到设置里填一个主机名，手机才能访问");
      return;
    }
    const qrUrl = scanBase + "?t=" + encodeURIComponent(d.token);
    drawPairQR(qrUrl);
    const cv = document.getElementById("qr-canvas");
    if (cv) cv.dataset.content = qrUrl;      // 方便自查二维码指向
    document.getElementById("btn-qr-copy").disabled = false;
    toast("二维码已生成（用 IP 直连，任何手机都能打开），30 分钟内有效");
    if (typeof loadPasswords === "function") await loadPasswords();
  } catch (e) {
    toast("生成失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText || "生成配对二维码";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const b1 = document.getElementById("btn-qr-create");
  if (b1) b1.addEventListener("click", makePairQR);
  const b2 = document.getElementById("btn-qr-copy");
  if (b2) b2.addEventListener("click", async () => {
    if (!pairCode) return;
    try {
      await navigator.clipboard.writeText(pairCode);
      toast("已复制配对码：" + pairCode);
    } catch (e) {
      toast("配对码：" + pairCode);
    }
  });
  api("/api/status").then((st) => {
    const el = document.getElementById("pair-url");
    if (el) el.textContent = pairPageUrlOf(st);
  }).catch(() => {});
});
