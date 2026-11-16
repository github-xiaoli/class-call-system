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
