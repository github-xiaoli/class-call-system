/* 班级叫号器 · 云端直连（原生界面版）
   流程：机器列表 → WebRTC 直连（信令走 Cloudflare Worker）→ DataChannel 登录叫号器
   → 所有业务（叫号/名条/屏幕/远控/文件/日志/设置）全部走 DataChannel 上的 WebRPC，
   服务器只负责转发信令，不经手任何业务数据。 */
(function () {
  const CFG = window.PORTAL_CONFIG || {};
  const API = CFG.apiBase || "";
  const WSB = CFG.wsBase || "";
  const $ = (id) => document.getElementById(id);

  let machines = [];
  let pc = null, ws = null, dc = null;
  let currentSn = "", currentName = "";
  let myId = "";
  let currentOnline = true;
  let relayNext = false;
  let connectRetry = 0;
  let connectTimer = null;
  const MAX_RETRY = 5;
  let rpcSeq = 0;
  const pending = new Map();
  let token = "";
  let screenWanted = false;
  let remoteOn = false, screenSize = null;
  let masterPwd = "";
  let curNumber = "";
  let rosterCache = [];

  window.__cqt = {
    get dc() { return dc; }, get pc() { return pc; },
    get state() { return pc ? pc.connectionState : "none"; },
  };

  /* ---------------- 基础工具 ---------------- */
  function show(view) {
    for (const v of ["view-list", "view-connecting", "view-login", "view-app",
                     "view-manage", "view-fallback"]) {
      const el = $(v);
      if (el) el.classList.toggle("hidden", v !== view);
    }
  }
  function setStep(text, mode) {
    const li = document.createElement("li");
    li.textContent = text;
    li.className = mode || "now";
    const box = $("steps");
    for (const el of box.children) el.className = "done";
    box.appendChild(li);
  }
  function resetSteps() { $("steps").innerHTML = ""; }
  function err(msg) { $("login-err").textContent = msg || ""; }
  function rememberKey(sn) { return "cc_remember_" + sn; }
  function linkState(text, ok) {
    const el = $("link-state");
    if (!el) return;
    el.textContent = text;
    el.className = "pill" + (ok ? " on" : "");
  }

  async function getJSON(url, opts) {
    const r = await fetch(url, Object.assign({ cache: "no-store" }, opts || {}));
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  /* ---------------- 机器管理（总密码只存内存） ---------------- */
  function manageMsg(t, ok) {
    const el = $("manage-msg");
    if (el) { el.textContent = t || ""; el.style.color = ok ? "#1a9d63" : "#c0392b"; }
  }
  async function manageUnlock() {
    const pwd = ($("in-master") || {}).value || "";
    if (!pwd) { $("manage-err").textContent = "请输入总密码"; return; }
    try {
      const r = await fetch(API + "/api/machines/verify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ master: pwd }),
      });
      const d = await r.json();
      if (!d.ok) { $("manage-err").textContent = d.error || "密码不对"; return; }
      masterPwd = pwd;
      $("in-master").value = "";
      $("manage-err").textContent = "";
      $("manage-lock").classList.add("hidden");
      $("manage-body").classList.remove("hidden");
      renderManage();
    } catch (e) { $("manage-err").textContent = "校验失败：" + e.message; }
  }
  function renderManage() {
    const ul = $("manage-list");
    if (!ul) return;
    ul.innerHTML = "";
    if (!machines.length) { ul.innerHTML = '<li style="cursor:default">还没有机器</li>'; return; }
    for (const m of machines) {
      const li = document.createElement("li");
      li.style.cursor = "default";
      li.innerHTML = '<div><div class="name"></div><div class="sn"></div></div>' +
        '<span class="badge ' + (m.online ? "on" : "off") + '">' +
        (m.online ? "在线" : "离线") + "</span>" +
        '<button class="btn ghost small" style="margin-left:8px">删除</button>';
      li.querySelector(".name").textContent = m.name || m.sn;
      li.querySelector(".sn").textContent = m.sn;
      li.querySelector("button").addEventListener("click", async () => {
        if (!confirm("删除机器 " + (m.name || m.sn) + "？删除后这台电脑需要重新注册。")) return;
        try {
          const r = await fetch(API + "/api/machines/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ master: masterPwd, sn: m.sn }),
          });
          const d = await r.json();
          manageMsg(d.ok ? "已删除" : (d.error || "删除失败"), d.ok);
          await loadMachines(); renderManage();
        } catch (e) { manageMsg("删除失败：" + e.message); }
      });
      ul.appendChild(li);
    }
  }
  async function manageCreate() {
    const name = ($("in-new-name") || {}).value || "";
    const sn = (($("in-new-sn") || {}).value || "").trim().toUpperCase();
    if (!name || !sn) { manageMsg("请填机器名称和 SN"); return; }
    try {
      const r = await fetch(API + "/api/machines/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ master: masterPwd, sn, name }),
      });
      const d = await r.json();
      manageMsg(d.ok ? ("已创建：" + sn) : (d.error || "创建失败"), d.ok);
      if (d.ok) { $("in-new-name").value = ""; $("in-new-sn").value = ""; }
      await loadMachines(); renderManage();
    } catch (e) { manageMsg("创建失败：" + e.message); }
  }

  /* ---------------- 机器列表 ---------------- */
  async function loadMachines() {
    const hint = $("list-hint");
    if (hint) hint.textContent = "正在读取机器列表…";
    try {
      const d = await getJSON(API + "/api/machines");
      machines = d.machines || [];
      render();
    } catch (e) {
      if (hint) hint.textContent = "读取失败：" + e.message + "（检查网络或稍后重试）";
    }
  }
  function render() {
    const ul = $("machines");
    ul.innerHTML = "";
    const hint = $("list-hint");
    if (!machines.length) {
      if (hint) hint.textContent = "还没有电脑注册。请在教室电脑上打开叫号器 → 设置 → 内网穿透 → 云端门户，填总密码完成注册。";
      return;
    }
    if (hint) hint.textContent = "共 " + machines.length + " 台，点一下即可直连。";
    for (const m of machines) {
      const li = document.createElement("li");
      li.innerHTML = '<div><div class="name"></div><div class="sn"></div></div>' +
        '<span class="badge ' + (m.online ? "on" : "off") + '">' +
        (m.online ? "在线" : "离线") + "</span>";
      li.querySelector(".name").textContent = m.name || m.sn;
      li.querySelector(".sn").textContent = m.sn + (m.version ? " · v" + m.version : "");
      li.addEventListener("click", () => connect(m));
      ul.appendChild(li);
    }
  }

  /* ---------------- WebRTC 直连 ---------------- */
  async function connect(m) {
    currentSn = m.sn;
    currentName = m.name || m.sn;
    if (m && m.online !== undefined && m.online !== null) {
      currentOnline = !(m.online === false || m.online === 0 ||
                        m.online === "0" || m.online === "false");
    }
    const offlineFast = !currentOnline;
    show("view-connecting");
    resetSteps();
    if (currentSn !== m.sn) connectRetry = 0;
    if (offlineFast) setStep("这台电脑现在显示离线，可能没开机或没联网…");
    setStep("读取 ICE 配置…");
    let ice = { iceServers: [] };
    try { ice = await getJSON(API + "/ice"); } catch (e) { /* 局域网直连也行 */ }

    setStep("连接信令服务器…");
    ws = new WebSocket(WSB + "/ws?sn=" + encodeURIComponent(m.sn) + "&role=web");
    ws.onmessage = onSignal;

    const forceRelay = relayNext || new URLSearchParams(location.search).get("relay") === "1";
    relayNext = false;
    pc = new RTCPeerConnection({ iceServers: ice.iceServers || [],
                                 iceTransportPolicy: forceRelay ? "relay" : "all" });
    window.__pc = pc;
    pc.onicecandidate = (ev) => {
      if (ev.candidate && ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "ice", peerId: myId, candidate: ev.candidate }));
      }
    };
    pc.ontrack = (ev) => {
      const v = $("screen");
      if (ev.streams && ev.streams[0]) v.srcObject = ev.streams[0];
      else { const ms = new MediaStream(); ms.addTrack(ev.track); v.srcObject = ms; }
      const tryPlay = () => { v.play().catch(() => {}); };
      tryPlay();
      ev.track.onunmute = tryPlay;
      const hint = $("screen-hint");
      if (hint) hint.style.display = "none";
    };
    pc.onconnectionstatechange = () => {
      const st = pc.connectionState;
      linkState(st === "connected" ? "已直连" : ("连接：" + st),
                st === "connected");
      if (st === "failed" || st === "disconnected") {
        linkState("直连中断", false);
      }
    };

    try { pc.addTransceiver("video", { direction: "recvonly" }); } catch (e) {}
    dc = pc.createDataChannel("rpc", { ordered: true });
    dc.onopen = () => {
      connectRetry = 0;
      if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
      setStep("直连已建立", "done");
      linkState("已直连", true);
      const saved = localStorage.getItem(rememberKey(currentSn));
      if (saved) autoLogin(saved);
      else show("view-login");
    };
    dc.onmessage = (ev) => onRpc(ev.data);

    const wsWatchdog = setTimeout(() => {
      if (ws && ws.readyState !== 1) {
        const maxTry = offlineFast ? 2 : MAX_RETRY;
        if (connectRetry < maxTry) {
          connectRetry += 1;
          setStep("信令连不上，第 " + connectRetry + "/" + maxTry + " 次重试…");
          try { ws.close(); } catch (e) {}
          teardown(false);
          setTimeout(() => connect({ sn: currentSn, name: currentName }), 800);
        } else {
          setStep("试了 " + maxTry + " 次仍连不上信令服务器");
          $("fallback-msg").textContent = "当前机器：" + currentName + "（" + currentSn + "）";
          show("view-fallback");
        }
      }
    }, 8000);
    ws.onerror = () => { setStep("信令连接失败"); };
    ws.onopen = async () => {
      clearTimeout(wsWatchdog);
      setStep("交换连接信息…");
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
    };

    connectTimer = setTimeout(() => {
      const opened = dc && dc.readyState === "open";
      const pcOk = pc && (pc.connectionState === "connected" ||
                          pc.iceConnectionState === "connected" ||
                          pc.iceConnectionState === "completed");
      if (opened || pcOk) return;
      const maxTry = offlineFast ? 2 : MAX_RETRY;
      if (connectRetry < maxTry) {
        connectRetry += 1;
        const useRelay = !forceRelay && connectRetry >= 2;
        setStep("没连上，第 " + connectRetry + "/" + maxTry + " 次自动重试"
                + (useRelay ? "（改走中继）" : "") + "…");
        teardown(false);
        setTimeout(() => {
          if (useRelay) relayNext = true;
          connect({ sn: currentSn, name: currentName });
        }, 800);
      } else {
        setStep(offlineFast
          ? "这台电脑现在显示离线（没开机或没联网）——请确认后重试，或改用电脑上显示的隧道地址"
          : "试了 " + maxTry + " 次仍连不上：网络可能封了 UDP，请改用教室电脑上显示的隧道地址（兼容模式）");
        $("fallback-msg").textContent = "当前机器：" + currentName + "（" + currentSn + "）";
        show("view-fallback");
      }
    }, offlineFast ? 8000 : (forceRelay ? 45000 : 20000));
  }

  async function onSignal(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "welcome") {
      myId = msg.you || msg.fromId || "";
      if (!msg.peers || !msg.peers.some((x) => x.role === "pc")) setStep("等待电脑上线…");
      return;
    }
    if (msg.type === "peer-joined") {
      if (msg.role === "web") return;
      setStep("电脑已在线");
      if (pc && dc && dc.readyState !== "open") {
        try {
          const o = await pc.createOffer();
          await pc.setLocalDescription(o);
          ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
          setStep("已重新发起连接…");
        } catch (e) { /* 忽略 */ }
      }
      return;
    }
    if (msg.type === "peer-left") { setStep("电脑断开了"); return; }
    if (msg.peerId && myId && msg.peerId !== myId) return;
    if (msg.type === "answer" && pc) {
      await pc.setRemoteDescription(msg.sdp);
      setStep("直连建立中…");
      return;
    }
    if (msg.type === "ice" && pc && msg.candidate) {
      try { await pc.addIceCandidate(msg.candidate); } catch (e) { /* 忽略 */ }
    }
  }

  /* ---------------- RPC（DataChannel 上的 WebRPC） ---------------- */
  function rpc(method, params) {
    return new Promise((resolve, reject) => {
      if (!dc || dc.readyState !== "open") { reject(new Error("还没连上电脑")); return; }
      const id = ++rpcSeq;
      pending.set(id, { resolve, reject });
      dc.send(JSON.stringify({ id, method, params: params || {}, token }));
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); reject(new Error("超时")); }
      }, 20000);
    });
  }
  function onRpc(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.ok) resolve(msg.result);
      else reject(new Error(msg.error || "操作失败"));
      return;
    }
    if (msg.event === "call") {
      const phrase = msg.data && msg.data.phrase ? msg.data.phrase : "";
      $("call-banner").textContent = phrase || "已叫号";
      speech(phrase);
    }
  }
  function speech(text) {
    try {
      if (!text || !window.speechSynthesis) return;
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "zh-CN";
      speechSynthesis.speak(u);
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 登录 ---------------- */
  async function doLogin(saved) {
    err("");
    const u = saved ? saved.u : $("in-user").value.trim();
    const p = saved ? saved.p : $("in-pass").value;
    if (!u || !p) { err("请填账号和密码"); return; }
    $("btn-login").disabled = true;
    try {
      const res = await rpc("auth.login", { username: u, password: p });
      token = res.token || "";
      if ($("chk-remember").checked || saved) {
        localStorage.setItem(rememberKey(currentSn), JSON.stringify({ u, p }));
      }
      $("app-title").textContent = currentName + "（" + currentSn + "）";
      show("view-app");
      await afterLogin();
    } catch (e) {
      err(e.message === "超时" ? "登录超时，请重试" : e.message);
      localStorage.removeItem(rememberKey(currentSn));
      show("view-login");
    } finally {
      $("btn-login").disabled = false;
    }
  }
  async function autoLogin(savedJson) {
    try {
      const saved = JSON.parse(savedJson);
      show("view-login");
      await doLogin(saved);
    } catch (e) { show("view-login"); }
  }

  /* ---------------- 原生界面：叫号 ---------------- */
  function buildNumpad() {
    const pad = $("numpad");
    pad.innerHTML = "";
    const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9",
                  "删除", "0", "清空"];
    for (const k of keys) {
      const b = document.createElement("button");
      b.textContent = k;
      b.className = k.length > 1 ? "np wide" : "np";
      b.addEventListener("click", () => {
        if (k === "删除") curNumber = curNumber.slice(0, -1);
        else if (k === "清空") curNumber = "";
        else if (curNumber.length < 12) curNumber += k;
        $("num-display").textContent = curNumber || " ";
      });
      pad.appendChild(b);
    }
  }
  async function doCall() {
    if (!curNumber) { $("call-banner").textContent = "请先输入学号"; return; }
    try {
      const r = await rpc("call", {
        number: curNumber,
        teacher: ($("in-teacher") || {}).value || "",
        force: true,
      });
      const phrase = (r && r.phrase) || "已叫号";
      $("call-banner").textContent = phrase;
      speech(phrase);
      curNumber = "";
      $("num-display").textContent = " ";
      await refreshCalls();
    } catch (e) {
      $("call-banner").textContent = "叫号失败：" + e.message;
    }
  }
  async function refreshCalls() {
    try {
      const r = await rpc("calls", {});
      const ul = $("call-list");
      ul.innerHTML = "";
      const list = (r && r.calls) || [];
      if (!list.length) { ul.innerHTML = '<li style="cursor:default">还没有叫号记录</li>'; return; }
      for (const c of list.slice(-30).reverse()) {
        const li = document.createElement("li");
        li.style.cursor = "pointer";
        li.innerHTML = '<div><div class="name"></div><div class="sn"></div></div>';
        li.querySelector(".name").textContent = (c.phrase || ("叫号 " + (c.number || "")));
        li.querySelector(".sn").textContent = (c.time || c.at || "");
        li.addEventListener("click", () => {
          curNumber = String(c.number || "");
          $("num-display").textContent = curNumber;
        });
        ul.appendChild(li);
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 原生界面：名条 ---------------- */
  async function refreshRoster() {
    try {
      const r = await rpc("roster.list", {});
      rosterCache = (r && r.roster) || [];
      const q = (($("in-roster-q") || {}).value || "").trim().toLowerCase();
      const ul = $("roster-list");
      ul.innerHTML = "";
      const list = rosterCache.filter((s) =>
        !q || String(s.number || "").toLowerCase().indexOf(q) >= 0 ||
        String(s.name || "").toLowerCase().indexOf(q) >= 0);
      if (!list.length) { ul.innerHTML = '<li style="cursor:default">没有匹配的名条</li>'; return; }
      for (const s of list) {
        const li = document.createElement("li");
        li.innerHTML = '<div><div class="name"></div><div class="sn"></div></div>' +
          '<button class="btn ghost small">删除</button>';
        li.querySelector(".name").textContent = s.name || "";
        li.querySelector(".sn").textContent = s.number || "";
        li.addEventListener("click", (ev) => {
          if (ev.target.tagName === "BUTTON") return;
          curNumber = String(s.number || "");
          $("num-display").textContent = curNumber;
          switchTab("tab-call");
        });
        li.querySelector("button").addEventListener("click", async () => {
          try {
            await rpc("roster.delete", { number: s.number });
            await refreshRoster();
          } catch (e) { alert("删除失败：" + e.message); }
        });
        ul.appendChild(li);
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 原生界面：屏幕 / 远控 ---------------- */
  async function toggleScreen() {
    screenWanted = !screenWanted;
    $("btn-screen").textContent = screenWanted ? "停止屏幕共享" : "开始屏幕共享";
    try {
      await rpc("screen", { on: screenWanted });
    } catch (e) {
      $("screen-hint").textContent = "屏幕开启失败：" + e.message;
    }
  }
  async function toggleRemote() {
    remoteOn = !remoteOn;
    try {
      if (remoteOn) {
        await rpc("remote.start", { teacher: ($("in-teacher") || {}).value || "老师" });
        const r = await rpc("remote.screen_size", {});
        screenSize = { w: (r && (r.width || r.w)) || 1920,
                       h: (r && (r.height || r.h)) || 1080 };
        $("btn-remote").textContent = "关闭远控";
        $("remote-state").textContent = "远控：开（点画面即可操作）";
      } else {
        await rpc("remote.stop", {});
        $("btn-remote").textContent = "开启远控";
        $("remote-state").textContent = "远控：关";
      }
    } catch (e) {
      remoteOn = false;
      $("btn-remote").textContent = "开启远控";
      $("remote-state").textContent = "远控失败：" + e.message;
    }
  }
  function videoPoint(ev) {
    const v = $("screen");
    const r = v.getBoundingClientRect();
    const w = (screenSize && screenSize.w) || 1920;
    const h = (screenSize && screenSize.h) || 1080;
    return { x: Math.round(((ev.clientX - r.left) / r.width) * w),
             y: Math.round(((ev.clientY - r.top) / r.height) * h) };
  }

  /* ---------------- 原生界面：文件 ---------------- */
  async function refreshFs() {
    const path = ($("fs-path") || {}).value || "";
    try {
      const r = await rpc("files.list", { path });
      const ul = $("fs-list");
      ul.innerHTML = "";
      const items = (r && (r.items || r.files || r.list)) || [];
      if (!items.length) { ul.innerHTML = '<li style="cursor:default">（空目录）</li>'; return; }
      for (const it of items) {
        const li = document.createElement("li");
        li.innerHTML = '<div><div class="name"></div><div class="sn"></div></div>';
        const isDir = it.is_dir || it.dir || it.type === "dir";
        li.querySelector(".name").textContent = (isDir ? "📁 " : "") + (it.name || "");
        li.querySelector(".sn").textContent = it.size != null ? (it.size + " 字节") : "";
        if (isDir) {
          li.style.cursor = "pointer";
          li.addEventListener("click", () => {
            $("fs-path").value = it.path || ((path ? path.replace(/[\\\/]$/, "") + "\\" : "") + it.name);
            refreshFs();
          });
        }
        ul.appendChild(li);
      }
    } catch (e) {
      $("fs-list").innerHTML = '<li style="cursor:default">' + e.message + "</li>";
    }
  }
  async function loadDrives() {
    try {
      const r = await rpc("files.drives", {});
      const list = (r && (r.drives || r.items)) || [];
      const sel = $("fs-drive");
      sel.innerHTML = "";
      for (const d of list) {
        const opt = document.createElement("option");
        opt.value = d.path || d.name || "";
        opt.textContent = d.name || d.path || "";
        sel.appendChild(opt);
      }
      if (list.length) { $("fs-path").value = list[0].path || list[0].name || ""; refreshFs(); }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 原生界面：日志 ---------------- */
  async function refreshLogs() {
    try {
      const r = await rpc("logs.tail", { limit: 200, q: ($("in-log-q") || {}).value || "" });
      const lines = (r && (r.lines || r.logs || r.items)) || [];
      const box = $("log-box");
      if (typeof lines[0] === "string") box.textContent = lines.join("\n");
      else box.textContent = lines.map((l) => l.text || JSON.stringify(l)).join("\n");
      box.scrollTop = box.scrollHeight;
    } catch (e) { $("log-box").textContent = "读取日志失败：" + e.message; }
  }

  /* ---------------- 原生界面：设置 ---------------- */
  const SETTING_FIELDS = [
    ["mdns_hostname", "设备名称（mDNS）", "text"],
    ["mdns_enabled", "启用 mDNS", "bool"],
    ["mdns_interface", "网卡", "text"],
    ["remote_gesture_mode", "远控手势模式（zoom_scroll / touch）", "text"],
    ["remote_touch_mode", "远控触摸模式（auto / touch / mouse）", "text"],
    ["monitor_quality", "监控画质（smooth / standard）", "text"],
    ["tts_enabled", "语音播报", "bool"],
    ["prompt_enabled", "叫号前提示音", "bool"],
    ["volume_enabled", "叫号时切换音量", "bool"],
    ["set_volume", "音量", "text"],
    ["set_play_count", "播报次数", "text"],
  ];
  async function loadSettings() {
    try {
      const r = await rpc("settings.get", {});
      const cfg = (r && r.config) || {};
      const box = $("settings-box");
      box.innerHTML = "";
      for (const [key, label, type] of SETTING_FIELDS) {
        if (!(key in cfg)) continue;
        const wrap = document.createElement("label");
        wrap.className = type === "bool" ? "checkbox" : "inline";
        const input = document.createElement("input");
        input.id = "cfg-" + key;
        input.dataset.key = key;
        input.dataset.type = type;
        if (type === "bool") { input.type = "checkbox"; input.checked = !!cfg[key]; }
        else { input.type = "text"; input.value = cfg[key] == null ? "" : String(cfg[key]); }
        wrap.appendChild(document.createTextNode(label));
        wrap.appendChild(input);
        box.appendChild(wrap);
      }
    } catch (e) { $("settings-box").textContent = "读取设置失败：" + e.message; }
  }
  async function saveSettings() {
    const cfg = {};
    for (const input of document.querySelectorAll("#settings-box input[data-key]")) {
      const k = input.dataset.key;
      cfg[k] = input.dataset.type === "bool" ? input.checked : input.value;
    }
    try {
      await rpc("settings.save", { config: cfg });
      $("settings-msg").textContent = "已保存（部分设置需重启生效）";
    } catch (e) { $("settings-msg").textContent = "保存失败：" + e.message; }
  }

  /* ---------------- 标签页 ---------------- */
  function switchTab(id) {
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("active", t.dataset.tab === id);
    }
    for (const p of document.querySelectorAll(".tabpane")) {
      p.classList.toggle("active", p.id === id);
    }
    if (id === "tab-roster") refreshRoster();
    if (id === "tab-files") { loadDrives(); }
    if (id === "tab-logs") refreshLogs();
    if (id === "tab-settings") loadSettings();
  }

  /* ---------------- 登录后加载数据 ---------------- */
  async function afterLogin() {
    buildNumpad();
    $("num-display").textContent = " ";
    await refreshCalls();
    try {
      const s = await rpc("status", {});
      if (s && s.version) $("app-title").textContent = currentName + " · v" + s.version;
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 事件绑定 ---------------- */
  function on(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }

  on("btn-refresh", "click", loadMachines);
  on("btn-manage", "click", () => {
    show("view-manage");
    masterPwd = "";
    $("manage-lock").classList.remove("hidden");
    $("manage-body").classList.add("hidden");
    $("in-master").value = "";
    $("manage-err").textContent = "";
    loadMachines().then(renderManage);
    setTimeout(() => { const el = $("in-master"); if (el) el.focus(); }, 100);
  });
  on("btn-manage-close", "click", () => { masterPwd = ""; $("in-master").value = ""; show("view-list"); });
  on("btn-unlock", "click", manageUnlock);
  on("in-master", "keydown", (e) => { if (e.key === "Enter") manageUnlock(); });
  on("btn-create", "click", manageCreate);
  on("btn-login", "click", () => doLogin(null));
  on("in-pass", "keydown", (e) => { if (e.key === "Enter") doLogin(null); });
  on("btn-cancel", "click", () => { teardown(); show("view-list"); });
  on("btn-back-list-1", "click", () => { teardown(); show("view-list"); loadMachines(); });
  on("btn-back-list-2", "click", () => { teardown(); show("view-list"); loadMachines(); });
  on("btn-exit", "click", () => {
    localStorage.removeItem(rememberKey(currentSn));
    teardown(); show("view-list"); loadMachines();
  });
  on("btn-open-fallback", "click", () => {
    const u = ($("in-fallback-url") || {}).value.trim();
    if (!/^https?:\/\//i.test(u)) { $("fallback-msg").textContent = "请填完整的 https 地址"; return; }
    window.open(u, "_blank");
  });
  on("btn-retry-connect", "click", () => { connectRetry = 0; connect({ sn: currentSn, name: currentName }); });

  on("btn-call", "click", doCall);
  on("btn-clear-num", "click", () => { curNumber = ""; $("num-display").textContent = " "; });
  on("btn-msg", "click", async () => {
    const text = prompt("要说什么？（电脑会语音播报）");
    if (!text) return;
    try {
      await rpc("message", { teacher: ($("in-teacher") || {}).value || "", text });
      $("call-banner").textContent = "已发送：" + text;
    } catch (e) { $("call-banner").textContent = "发送失败：" + e.message; }
  });
  on("btn-roster-refresh", "click", refreshRoster);
  on("btn-roster-add", "click", async () => {
    const number = ($("in-roster-number") || {}).value.trim();
    const name = ($("in-roster-name") || {}).value.trim();
    if (!number || !name) { alert("请填学号和姓名"); return; }
    try {
      await rpc("roster.add", { number, name });
      $("in-roster-number").value = ""; $("in-roster-name").value = "";
      await refreshRoster();
    } catch (e) { alert("添加失败：" + e.message); }
  });
  on("btn-roster-clear", "click", async () => {
    if (!confirm("清空全部名条？")) return;
    try { await rpc("roster.clear", {}); await refreshRoster(); }
    catch (e) { alert("清空失败：" + e.message); }
  });
  on("btn-screen", "click", toggleScreen);
  on("btn-remote", "click", toggleRemote);
  on("btn-fs-refresh", "click", refreshFs);
  on("btn-fs-up", "click", () => {
    const p = ($("fs-path") || {}).value || "";
    const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
    $("fs-path").value = i > 0 ? p.slice(0, i) : "";
    refreshFs();
  });
  on("fs-drive", "change", () => { $("fs-path").value = $("fs-drive").value; refreshFs(); });
  on("btn-logs-refresh", "click", refreshLogs);
  on("btn-settings-save", "click", saveSettings);

  on("screen", "click", async (ev) => {
    if (!remoteOn) return;
    const p = videoPoint(ev);
    try {
      await rpc("remote.input", { input: { action: "absmove", x: p.x, y: p.y } });
      await rpc("remote.input", { input: { action: "click", button: "left" } });
    } catch (e) { $("remote-state").textContent = "远控点击失败：" + e.message; }
  });
  on("screen", "wheel", async (ev) => {
    if (!remoteOn) return;
    ev.preventDefault();
    try {
      await rpc("remote.input", { input: { action: "scroll", delta: ev.deltaY > 0 ? -3 : 3 } });
    } catch (e) { /* 忽略 */ }
  }, { passive: false });

  for (const t of document.querySelectorAll(".tab")) {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  }

  function teardown(resetView = true) {
    if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
    try { if (dc) dc.close(); } catch (e) {}
    try { if (pc) pc.close(); } catch (e) {}
    try { if (ws) ws.close(); } catch (e) {}
    dc = null; pc = null; ws = null; token = "";
    remoteOn = false; screenWanted = false; curNumber = "";
    const v = $("screen"); if (v) v.srcObject = null;
    const b = $("btn-screen"); if (b) b.textContent = "开始屏幕共享";
    const r = $("btn-remote"); if (r) r.textContent = "开启远控";
    const rs = $("remote-state"); if (rs) rs.textContent = "远控：关";
    linkState("未连接", false);
    if (!resetView) return;
  }

  buildNumpad();
  loadMachines();
  setInterval(() => {
    if (!$("view-list").classList.contains("hidden")) loadMachines();
  }, 20000);
})();
