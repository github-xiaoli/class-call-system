/* 文件传输 */
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
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

let currentPath = "";
let pending = null; // {action:'copy'|'move', path, name}

function handleAuthError(e) {
  if (e && (e.code === "NEED_ACTIVATE" || /未激活|需要密码激活|请先激活/.test(e.message || ""))) {
    toast("请先激活");
    if (confirm("当前设备未激活。是否返回主页完成激活？")) location.href = "/";
    return false;
  }
  return true;
}

async function fsGet(url) {
  const res = await Auth.fetchAuthed(url);
  if (res.status === 401) { throw Object.assign(new Error("需要密码激活后才能使用该功能"), { code: "NEED_ACTIVATE" }); }
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

async function fsPost(url, body) {
  const res = await Auth.fetchAuthed(url, {
    method: "POST",
    headers: body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    body: body instanceof FormData ? body : JSON.stringify(body),
  });
  if (res.status === 401) { throw Object.assign(new Error("需要密码激活后才能使用该功能"), { code: "NEED_ACTIVATE" }); }
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

async function loadDrives() {
  try {
    const d = await fsGet("/api/fs/drives");
    const sel = $("drive-select");
    sel.innerHTML = "";
    for (const dr of d.drives) sel.add(new Option(dr.name, dr.path));
    if (d.drives.length) { currentPath = d.drives[0].path; await refresh(); }
  } catch (e) {
    if (handleAuthError(e)) toast("加载磁盘失败：" + e.message);
  }
}

function renderPending() {
  $("btn-paste").classList.toggle("hidden", !pending);
  $("btn-cancel-op").classList.toggle("hidden", !pending);
  $("pending-hint").textContent = pending
    ? (" · 待" + (pending.action === "copy" ? "复制" : "移动") + "：" + pending.name)
    : "";
}

async function refresh() {
  if (!currentPath) return;
  $("fs-empty").textContent = "加载中…";
  $("fs-empty").classList.remove("hidden");
  try {
    const d = await fsGet("/api/fs/list?path=" + encodeURIComponent(currentPath));
    currentPath = d.path;
    $("cur-path").textContent = d.path;
    renderList(d.items || []);
    renderPending();
  } catch (e) {
    if (handleAuthError(e)) toast("读取失败：" + e.message);
    $("fs-empty").textContent = "读取失败";
  }
}

function renderList(items) {
  const tb = $("fs-tbody");
  tb.innerHTML = "";
  $("fs-empty").classList.toggle("hidden", items.length > 0);
  $("fs-empty").textContent = "此文件夹为空";
  for (const it of items) {
    const tr = document.createElement("tr");
    const icon = it.is_dir ? "📁" : "📄";
    tr.innerHTML =
      '<td class="fs-name" data-path="' + esc(it.path) + '" data-dir="' + (it.is_dir ? 1 : 0) + '">' +
        icon + " " + esc(it.name) + "</td>" +
      '<td>' + (it.is_dir ? "" : fmtSize(it.size)) + "</td>" +
      '<td class="fs-mtime">' + esc(it.mtime) + "</td>" +
      '<td class="fs-actions">' +
        (it.is_dir ? "" : '<button class="mini" data-op="download" data-path="' + esc(it.path) + '">下载</button>') +
        '<button class="mini" data-op="rename" data-path="' + esc(it.path) + '">重命名</button>' +
        '<button class="mini" data-op="copy" data-path="' + esc(it.path) + '">复制</button>' +
        '<button class="mini" data-op="move" data-path="' + esc(it.path) + '">移动</button>' +
        '<button class="mini danger" data-op="delete" data-path="' + esc(it.path) + '">删除</button>' +
      "</td>";
    tr.querySelector(".fs-name").addEventListener("dblclick", () => {
      if (it.is_dir) { currentPath = it.path; refresh(); }
    });
    if (it.is_dir) {
      tr.querySelector(".fs-name").style.cursor = "pointer";
      tr.querySelector(".fs-name").title = "双击进入";
    }
    tb.appendChild(tr);
  }
  tb.querySelectorAll("button[data-op]").forEach((b) => {
    b.addEventListener("click", () => onOp(b.dataset.op, b.dataset.path));
  });
}

async function onOp(op, path) {
  const name = path.split(/[\\/]/).filter(Boolean).pop() || path;
  try {
    if (op === "download") {
      const url = await Auth.urlWithToken("/api/fs/download?path=" + encodeURIComponent(path));
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }
    if (op === "rename") {
      const nn = prompt("重命名为：", name);
      if (!nn || nn === name) return;
      await fsPost("/api/fs/rename", { path, new_name: nn });
      toast("已重命名");
    } else if (op === "delete") {
      if (!confirm("确定删除 “" + name + "” 吗？此操作不可撤销。")) return;
      await fsPost("/api/fs/delete", { paths: [path] });
      toast("已删除");
    } else if (op === "copy" || op === "move") {
      pending = { action: op, path, name };
      toast("已选择，请进入目标文件夹后点“粘贴到这里”");
      renderPending();
      return;
    }
    await refresh();
  } catch (e) {
    if (handleAuthError(e)) toast("操作失败：" + e.message);
  }
}

async function pasteHere() {
  if (!pending) return;
  try {
    await fsPost("/api/fs/" + pending.action, { path: pending.path, dest_dir: currentPath });
    toast("已" + (pending.action === "copy" ? "复制" : "移动") + "到当前目录");
    pending = null;
    await refresh();
  } catch (e) {
    if (handleAuthError(e)) toast("粘贴失败：" + e.message);
  }
}

async function uploadFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append("path", currentPath);
  for (const f of files) fd.append("files", f);
  try {
    const d = await fsPost("/api/fs/upload", fd);
    toast("上传完成：" + d.saved + " 个");
    await refresh();
  } catch (e) {
    if (handleAuthError(e)) toast("上传失败：" + e.message);
  }
}

/* ---------- 事件 ---------- */
$("drive-select").addEventListener("change", (e) => { currentPath = e.target.value; refresh(); });
$("btn-up").addEventListener("click", async () => {
  if (!currentPath) return;
  const parent = currentPath.split(/[\\/]/).slice(0, -1).join("\\");
  if (parent && parent !== currentPath) { currentPath = parent + "\\"; await refresh(); }
});
$("btn-refresh").addEventListener("click", refresh);
$("btn-mkdir").addEventListener("click", async () => {
  const n = prompt("新建文件夹名称：");
  if (!n) return;
  try { await fsPost("/api/fs/mkdir", { path: currentPath, name: n }); toast("已创建"); await refresh(); }
  catch (e) { if (handleAuthError(e)) toast("创建失败：" + e.message); }
});
$("btn-paste").addEventListener("click", pasteHere);
$("btn-cancel-op").addEventListener("click", () => { pending = null; renderPending(); });
$("file-upload").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });

const dropZone = $("drop-zone");
dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag");
  uploadFiles(e.dataTransfer.files);
});

loadDrives();
