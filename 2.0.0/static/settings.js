/* 设置子页面脚本：读写 /api/settings + 内网穿透面板 */
(function () {
  const $ = (id) => document.getElementById(id);
  const jget = (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json());
  const jpost = (u, b) => fetch(u, {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify(b || {}),
  }).then((r) => r.json());


  /* ---------- 退出 GitHub 登录（事件委托，确保一定生效） ---------- */
  document.addEventListener("click", async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("#btn-gh-logout") : null;
    if (!btn) return;
    ev.preventDefault();
    const clear = confirm("退出 GitHub 登录？\n\n点「确定」= 退出并删除本机保存的账号密码\n点「取消」= 只退出登录，密码保留在本机");
    note(clear ? "正在退出并清除本机密码…" : "正在退出登录…");
    btn.disabled = true;
    try {
      const r = await jpost("/api/tunnel/logout", { clear_credentials: clear });
      if (!r.ok) { note(r.error || "退出失败", "err"); return; }
      note(r.logged_in ? "已退出，但仍检测到登录态"
                       : ("已退出 GitHub 登录" + (clear ? "，本机密码已删除" : "")),
           r.logged_in ? "err" : "ok");
      const gh = $("set-gh-user"); if (gh) gh.value = "";
      const pw = $("set-gh-pwd"); if (pw) pw.value = "";
      refreshTunnel();
    } catch (e) {
      note("退出失败：" + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });


  /* ---------- 手动登录（设备码，备用通道） ---------- */
  document.addEventListener("click", async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("#btn-tunnel-login") : null;
    if (!btn) return;
    ev.preventDefault();
    btn.disabled = true;
    note("正在申请设备码…");
    try {
      const d = await fetch("/api/tunnel/login/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin", body: "{}",
      }).then((r) => r.json());
      const t = (d && d.tunnel) || d || {};
      const code = t.user_code || t.userCode || (d && d.user_code) || "";
      const box = $("tunnel-login-box");
      if (box) box.style.display = "";
      const el = $("tunnel-user-code");
      if (el) el.textContent = code || "--------";
      note(code ? ("设备码 " + code + " —— 请到 github.com/login/device 输入")
                : "已发起登录，请按页面提示操作", code ? "ok" : "");
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        let st = {};
        try { st = (await jget("/api/tunnel/status")).tunnel || {}; } catch (e) { break; }
        if (st.user_code) {
          const cel = $("tunnel-user-code");
          if (cel) cel.textContent = st.user_code;
          note("设备码 " + st.user_code + " —— 请到 github.com/login/device 输入", "ok");
        }
        if (st.login_state === "logged_in") { note("手动登录成功", "ok"); refreshTunnel(); break; }
        if (st.login_state === "error") { note("手动登录失败：" + (st.login_error || ""), "err"); break; }
      }
    } catch (e) {
      note("申请设备码失败：" + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });

  /* ---------- 有头/无头 + 进度显示 ---------- */
  function showProgress(step, page) {
    const box = $("auto-progress");
    if (!box) return;
    if (!step) { box.style.display = "none"; return; }
    box.style.display = "";
    const s1 = $("auto-step"), s2 = $("auto-page");
    if (s1) s1.textContent = "当前步骤：" + step;
    if (s2) s2.textContent = page ? ("页面：" + page) : "";
  }

  (async function initHeaded() {
    const cb = $("chk-gh-headed");
    if (!cb) return;
    try {
      const st = await jget("/api/tunnel/status");
      cb.checked = st.headed !== false;
    } catch (e) { cb.checked = true; }
    cb.addEventListener("change", async () => {
      await jpost("/api/tunnel/autologin_mode", { headed: !!cb.checked });
      note(cb.checked ? "登录时会显示浏览器窗口" : "登录时后台运行（不显示窗口）", "ok");
    });
  })();

  /* 返回主页 + 左边缘悬停出应用列表 */
  if ($("sub-back")) $("sub-back").addEventListener("click", () => { location.href = "/"; });
  const edge = $("edge-hot");
  if (edge) edge.addEventListener("mouseenter", () => document.body.classList.add("edge-open"));
  const tab = $("edge-tab");
  if (tab) tab.addEventListener("click", () => document.body.classList.add("edge-open"));
  const closeBtn = $("drawer-close");
  if (closeBtn) closeBtn.addEventListener("click", () => document.body.classList.remove("edge-open", "drawer-open"));
  const mask = $("drawer-mask");
  if (mask) mask.addEventListener("click", () => document.body.classList.remove("edge-open", "drawer-open"));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.body.classList.remove("edge-open", "drawer-open");
  });

  /* ---------- 配置读写 ---------- */
  const FIELDS = [
    ["set-phrase", "phrase", "text"], ["set-play-count", "play_count", "int"],
    ["set-volume-enabled", "volume_enabled", "bool"], ["set-volume", "volume", "int"],
    ["set-prompt-enabled", "prompt_enabled", "bool"], ["set-prompt-sound", "prompt_sound", "text"],
    ["set-tts-engine", "tts_engine", "text"], ["set-autostart", "autostart", "bool"],
    ["set-mdns-enabled", "mdns_enabled", "bool"], ["set-mdns-hostname", "mdns_hostname", "text"],
    ["set-port", "port", "int"], ["set-mdns-interface", "mdns_interface", "text"],
    ["set-monitor-quality", "monitor_quality", "text"], ["set-remote-touch", "remote_touch_mode", "text"],
    ["set-gesture", "remote_gesture_mode", "text"], ["set-secure-port", "secure_port", "int"],
    ["set-secure-enabled", "secure_enabled", "bool"],
  ];

  async function loadSettings() {
    let cfg = {};
    try { cfg = await jget("/api/settings"); } catch (e) { return; }
    cfg = cfg.config || cfg;
    for (const [id, key, type] of FIELDS) {
      const el = $(id);
      if (!el) continue;
      const v = cfg[key];
      if (type === "bool") el.checked = !!v;
      else el.value = (v === undefined || v === null) ? "" : v;
    }
    const gh = $("set-gh-user");
    if (gh && cfg.creds_user) gh.value = cfg.creds_user;
  }

  if ($("btn-save-settings")) $("btn-save-settings").addEventListener("click", async () => {
    const msg = $("save-msg");
    const cfg = {};
    for (const [id, key, type] of FIELDS) {
      const el = $(id);
      if (!el) continue;
      if (type === "bool") cfg[key] = !!el.checked;
      else if (type === "int") cfg[key] = parseInt(el.value, 10) || 0;
      else cfg[key] = String(el.value || "").trim();
    }
    if (msg) msg.textContent = "保存中…";
    try {
      const r = await jpost("/api/settings", cfg);
      if (msg) msg.textContent = (r && r.ok === false) ? ("保存失败：" + (r.error || "")) : "已保存";
    } catch (e) {
      if (msg) msg.textContent = "保存失败：" + e.message;
    }
  });

  /* ---------- 内网穿透 ---------- */
  function note(text, kind) {
    const el = $("auto-msg") || $("name-msg");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = kind === "err" ? "#b3261e" : (kind === "ok" ? "#12805c" : "#6b7590");
  }

  async function refreshTunnel() {
    let d;
    try { d = await jget("/api/tunnel/status"); } catch (e) { return; }
    const t = d.tunnel || {};
    const badge = $("tunnel-host-state");
    if (badge) {
      const m = { running: ["运行中", "ok"], starting: ["启动中", "warn"], error: ["出错", "danger"] };
      const l = m[t.host_state] || ["未开启", "gray"];
      badge.textContent = l[0];
      badge.className = "badge " + l[1];
    }
    const cbOn = $("chk-tunnel-on");
    if (cbOn) cbOn.checked = !!(d.enabled || t.host_state === "running" || t.host_state === "starting");
    const cbAuto = $("chk-tunnel-auto");
    if (cbAuto && typeof d.autostart === "boolean") cbAuto.checked = d.autostart;
    const box = $("tunnel-url-box"), link = $("tunnel-url"), qrBox = $("tunnel-qr");
    if (t.url) {
      if (link) { link.textContent = t.url; link.href = t.url; }
      if (box) box.style.display = "";
      if (qrBox && typeof qrcode === "function") {
        try {
          const q = qrcode(0, "M"); q.addData(t.url); q.make();
          qrBox.innerHTML = q.createImgTag(4, 8);
        } catch (e) { qrBox.textContent = t.url; }
      }
    } else if (box) {
      box.style.display = "none";
    }
  }

  async function refreshNames() {
    const box = $("tunnel-list");
    if (!box) return;
    let d;
    try { d = await jget("/api/tunnel/list_names"); } catch (e) {
      box.textContent = "读取失败：" + e.message;
      return;
    }
    if (!d.ok) {
      box.innerHTML = '<span style="color:#b45309">' + (d.error || "读取失败") +
        "</span>（点上面的「保存并登录」或展开手动登录先登录）";
      return;
    }
    const cur = (d.current || "").trim();
    const list = d.tunnels || [];
    if (!list.length) {
      box.textContent = "这个账号下还没有隧道，在下面填个名字点「新建隧道」。";
      return;
    }
    let html = '<table style="width:100%;border-collapse:collapse;font-size:13.5px">' +
      '<thead><tr>' +
      '<th style="text-align:left;padding:6px 4px;border-bottom:1px solid #e6ebf5">隧道名字</th>' +
      '<th style="width:64px;padding:6px 4px;border-bottom:1px solid #e6ebf5">状态</th>' +
      '<th style="width:132px;padding:6px 4px;border-bottom:1px solid #e6ebf5;text-align:right">操作</th>' +
      "</tr></thead><tbody>";
    for (const n of list) {
      const isCur = n === cur;
      html += "<tr>" +
        '<td style="padding:7px 4px;border-bottom:1px solid #f2f5fb;font-family:Consolas,monospace">' +
        n + "</td>" +
        '<td style="padding:7px 4px;border-bottom:1px solid #f2f5fb">' +
        (isCur ? '<span class="badge ok">当前</span>' : "") + "</td>" +
        '<td style="padding:7px 4px;border-bottom:1px solid #f2f5fb;text-align:right">' +
        '<button class="btn ghost" data-use="' + n + '" style="padding:4px 10px;font-size:12.5px">使用</button> ' +
        '<button class="btn danger" data-del="' + n + '" style="padding:4px 10px;font-size:12.5px">删除</button>' +
        "</td></tr>";
    }
    box.innerHTML = html + "</tbody></table>";

    box.querySelectorAll("button[data-use]").forEach((b) => {
      b.addEventListener("click", async () => {
        const name = b.getAttribute("data-use");
        const tid = $("set-tunnel-id");
        if (tid) tid.value = name;
        note("正在切到 " + name + "…");
        const r = await jpost("/api/tunnel/create_async", { tunnel_id: name });
        if (!r.ok) { note(r.error || "切换失败", "err"); return; }
        const job = await waitJob();
        note(job.ok ? ("已切换到 " + name) : ("失败：" + (job.error || "")), job.ok ? "ok" : "err");
        refreshNames();
        refreshTunnel();
      });
    });
    box.querySelectorAll("button[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        const name = b.getAttribute("data-del");
        if (!confirm("删除隧道 " + name + "？这个地址会失效。")) return;
        note("正在删除 " + name + "…");
        const r = await jpost("/api/tunnel/delete_name", { tunnel_id: name });
        note(r.ok ? ("已删除 " + name) : (r.error || "删除失败"), r.ok ? "ok" : "err");
        refreshNames();
      });
    });
  }

  async function waitJob() {
    for (let i = 0; i < 40; i++) {
      let j = {};
      try { j = (await jget("/api/tunnel/job")).job || {}; } catch (e) { break; }
      if (!j.running) return j;
      await new Promise((r) => setTimeout(r, 1200));
    }
    return {};
  }

  async function waitAuto(maxSec) {
    const end = Date.now() + (maxSec || 150) * 1000;
    while (Date.now() < end) {
      let s = {};
      try { s = (await jget("/api/tunnel/auto_status")).auto || {}; } catch (e) { break; }
      showProgress(s.step || (s.state === "running" ? s.message : ""), s.page || "");
      const st = String(s.state || "");
      if (st === "ok" || st === "fail" || st === "error") { showProgress(""); return s; }
      await new Promise((r) => setTimeout(r, 1500));
    }
    return null;
  }

  if ($("btn-auto-login")) $("btn-auto-login").addEventListener("click", async () => {
    const u = ($("set-gh-user") || {}).value || "";
    const p = ($("set-gh-pwd") || {}).value || "";
    if (!u || !p) { note("请先填 GitHub 账号和密码", "err"); return; }
    note("正在登录 GitHub，请不要关闭这个页面（通常 30~60 秒）…");
    const r = await jpost("/api/tunnel/login_auto", { username: u, password: p });
    if (!r.ok) { note(r.error || "启动登录失败", "err"); return; }
    if (r.message === "已经是登录状态") { note("已经是登录状态", "ok"); refreshTunnel(); return; }
    const s = await waitAuto(180);
    if (!s) { note("登录还在进行中，稍后再点一次「重新登录」看结果", "err"); return; }
    if (s.state === "ok") {
      note("登录成功：" + (s.user || s.logged_in || ""), "ok");
      refreshTunnel();
      return;
    }
    const msg = String(s.message || "登录失败");
    if (msg.indexOf("NEED_2FA") >= 0 || msg.indexOf("两步验证") >= 0) {
      note(msg.replace("NEED_2FA|", ""), "err");
    } else if (msg.indexOf("NEED_EMAIL") >= 0 || msg.indexOf("邮箱验证") >= 0) {
      note(msg.replace("NEED_EMAIL|", ""), "err");
    } else {
      note("登录失败：" + msg, "err");
    }
  });

  if ($("btn-relogin")) $("btn-relogin").addEventListener("click", async () => {
    note("正在强制重新登录…");
    const r = await jpost("/api/tunnel/relogin", {});
    if (!r.ok) { note(r.error || "失败", "err"); return; }
    const s = await waitAuto(180);
    if (s && s.state === "ok") { note("已重新登录：" + (s.user || s.logged_in || ""), "ok"); }
    else if (s) { note("失败：" + String(s.message || "").replace(/^NEED_(2FA|EMAIL)\|/, ""), "err"); }
    else { note("还在进行中，稍后再看结果", "err"); }
    refreshTunnel();
  });

  if ($("btn-create-name")) $("btn-create-name").addEventListener("click", async () => {
    const tid = ($("set-tunnel-id") || {}).value || "";
    if (!tid) { note("先填隧道名字", "err"); return; }
    note("正在新建/切换隧道…");
    const r = await jpost("/api/tunnel/create_async", { tunnel_id: tid });
    if (!r.ok) { note(r.error || "失败", "err"); return; }
    const j = await waitJob();
    note(j.ok ? ("已切换到 " + tid) : ("失败：" + (j.error || "")), j.ok ? "ok" : "err");
    refreshTunnel(); refreshNames();
  });

  if ($("chk-tunnel-on")) $("chk-tunnel-on").addEventListener("change", async (e) => {
    const on = e.target.checked;
    const r = await jpost("/api/tunnel/enable_async", { on: on });
    if (!r.ok) { e.target.checked = !on; note(r.error || "失败", "err"); return; }
    const j = await waitJob();
    note(j.ok ? (on ? "已开启" : "已关闭") : ("失败：" + (j.error || "")), j.ok ? "ok" : "err");
    refreshTunnel();
  });

  if ($("chk-tunnel-auto")) $("chk-tunnel-auto").addEventListener("change", async (e) => {
    await jpost("/api/tunnel/autostart", { on: e.target.checked });
  });

  /* 按 ?g= 滚到对应分组 */
  const g = new URLSearchParams(location.search).get("g");
  if (g) {
    [...document.querySelectorAll(".set-group")].forEach((h) => {
      if (h.textContent.indexOf(g) >= 0) {
        setTimeout(() => h.scrollIntoView({ block: "start", behavior: "smooth" }), 300);
      }
    });
  }

  loadSettings();
  refreshTunnel();
  refreshNames();
  setInterval(refreshTunnel, 8000);
})();

  /* 左边缘 16px 内自动滑出；移到中间（>300px）自动收起 */
  document.addEventListener("mousemove", (e) => {
    const open = document.body.classList.contains("edge-open");
    if (!open && e.clientX <= 16) document.body.classList.add("edge-open");
    else if (open && e.clientX > 300) document.body.classList.remove("edge-open");
  });

  /* 高亮当前所在的应用 */
  (function markActive() {
    const here = location.pathname.replace(/\/$/, "") || "/";
    document.querySelectorAll(".app-item").forEach((a) => {
      const href = (a.getAttribute("href") || "").replace(/\/$/, "") || "/";
      if (href === here) a.classList.add("active");
      if (a.classList.contains("home")) a.classList.toggle("active", here === "/");
    });
  
  async function fillGhUser() {
    const el = $("set-gh-user");
    if (!el) return;
    try {
      const s = (await jget("/api/tunnel/auto_status")).auto || {};
      const u = s.cred_user || s.user || s.logged_in || "";
      if (u && !el.value) el.value = u;
      el.placeholder = "填 GitHub 账号（如 classcaller-net）";
    } catch (e) { /* 忽略 */ }
  }
  fillGhUser();
})();
