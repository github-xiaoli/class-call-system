/* 设置子页面脚本：读写 /api/settings + 内网穿透面板 */
(function () {
  const $ = (id) => document.getElementById(id);
  const jget = (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json());
  const jpost = (u, b) => fetch(u, {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify(b || {}),
  }).then((r) => r.json());

  /* 返回主页 + 左边缘悬停出应用列表 */
  if ($("sub-back")) $("sub-back").addEventListener("click", () => { location.href = "/"; });
  const edge = $("edge-hot");
  if (edge) edge.addEventListener("mouseenter", () => document.body.classList.add("edge-open"));
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
    try { d = await jget("/api/tunnel/list_names"); } catch (e) { return; }
    if (!d.ok) { box.textContent = d.error || "读取失败"; return; }
    const cur = (d.current || "").trim();
    const list = d.tunnels || [];
    box.innerHTML = list.length
      ? list.map((n) => '<div class="hint" style="display:flex;gap:8px;align-items:center">' +
          '<code style="min-width:120px">' + n + "</code>" +
          (n === cur ? '<span class="badge ok">当前</span>' : "") + "</div>").join("")
      : "还没有隧道，在下面填个名字新建。";
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

  if ($("btn-auto-login")) $("btn-auto-login").addEventListener("click", async () => {
    const u = ($("set-gh-user") || {}).value || "";
    const p = ($("set-gh-pwd") || {}).value || "";
    if (!u || !p) { note("请先填 GitHub 账号和密码", "err"); return; }
    note("正在登录 GitHub（约 30~60 秒）…");
    const r = await jpost("/api/tunnel/login_auto", { username: u, password: p });
    note(r.ok ? "登录成功" : ("失败：" + (r.error || "")), r.ok ? "ok" : "err");
    refreshTunnel();
  });

  if ($("btn-relogin")) $("btn-relogin").addEventListener("click", async () => {
    note("正在强制重新登录…");
    const r = await jpost("/api/tunnel/relogin", {});
    if (!r.ok) { note(r.error || "失败", "err"); return; }
    const j = await waitJob();
    note(j.ok ? "已重新登录" : ("失败：" + (j.error || "")), j.ok ? "ok" : "err");
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
