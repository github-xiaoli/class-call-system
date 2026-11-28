/* 班级叫号器 · 云端门户
   流程：机器列表 → WebRTC 直连（信令走 Worker）→ DataChannel 里登录叫号器 → 业务 RPC
   业务数据（叫号、名条、屏幕、远控、文件）全部走 DataChannel，不经过服务器。 */
(function () {
  const CFG = window.PORTAL_CONFIG || {};
  const API = CFG.apiBase || "";
  const WSB = CFG.wsBase || "";
  const $ = (id) => document.getElementById(id);

  let machines = [];
  let pc = null, ws = null, dc = null;
  let currentSn = "", currentName = "";
  let myId = "";
  let currentOnline = true;   // 记住机器在线状态，重试时也别丢
  window.__cqt = { get dc() { return dc; }, get pc() { return pc; },
                  get state() { return pc ? pc.connectionState : 'none'; } };
  let connectRetry = 0;
  let connectTimer = null;
  const MAX_RETRY = 5;   // 我在信令房间里的 id，用于多老师并发时精确收发
  let rpcSeq = 0;
  const pending = new Map();
  let token = "";
  let screenWanted = false;


  /* ---------------- 机器管理（总密码只存内存，每次都要输） ---------------- */
  let masterPwd = "";   // 只在内存里，刷新/关闭即消失

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
      $("in-master").value = "";          // 输入框也不留
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
        '<span class="badge ' + (m.online ? "on" : "off") + '">' + (m.online ? "在线" : "离线") + "</span>" +
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

  /* ---------------- 工具 ---------------- */
  function show(view) {
    for (const v of ["view-list", "view-connecting", "view-login", "view-app", "view-iframe", "view-manage", "view-fallback"]) {
      $(v).classList.toggle("hidden", v !== view);
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

  async function getJSON(url, opts) {
    const r = await fetch(url, Object.assign({ cache: "no-store" }, opts || {}));
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  /* ---------------- 机器列表 ---------------- */
  async function loadMachines() {
    $("list-tip").textContent = "正在读取机器列表…";
    try {
      const d = await getJSON(API + "/api/machines");
      machines = d.machines || [];
      render();
    } catch (e) {
      $("list-tip").textContent = "读取失败：" + e.message + "（检查网络或稍后重试）";
    }
  }

  function render() {
    const ul = $("machines");
    ul.innerHTML = "";
    if (!machines.length) {
      $("list-tip").textContent = "还没有电脑注册。请在教室电脑上打开叫号器 → 设置 → 内网穿透 → 云端门户，填总密码完成注册。";
      return;
    }
    $("list-tip").textContent = "共 " + machines.length + " 台，点一下即可直连。";
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
    if (offlineFast) {
      resetSteps();
      setStep("这台电脑现在显示离线，可能没开机或没联网…");
    }
    show("view-connecting");
    resetSteps();
    if (currentSn !== m.sn) connectRetry = 0;
    setStep("读取 ICE 配置…");
    let ice = { iceServers: [] };
    try { ice = await getJSON(API + "/ice"); } catch (e) { /* 无 STUN 也能局域网直连 */ }

    setStep("连接信令服务器…");
    ws = new WebSocket(WSB + "/ws?sn=" + encodeURIComponent(m.sn) + "&role=web");
    ws.onmessage = onSignal;

    const forceRelay = new URLSearchParams(location.search).get("relay") === "1";
    pc = new RTCPeerConnection({ iceServers: ice.iceServers || [],
                                 iceTransportPolicy: forceRelay ? "relay" : "all" });
    if (forceRelay) setStep("已开启「仅中继」模式（测试用）");
    window.__pc = pc;   // 便于诊断（无副作用）
    pc.onicecandidate = (ev) => {
      if (ev.candidate && ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "ice", peerId: myId, candidate: ev.candidate }));
      }
    };
    pc.ontrack = (ev) => {
      const v = $("screen");
      // aiortc 往往不带 stream id，必须自己拼一个 MediaStream，否则视频永远是 0x0
      if (ev.streams && ev.streams[0]) {
        v.srcObject = ev.streams[0];
      } else {
        const ms = new MediaStream();
        ms.addTrack(ev.track);
        v.srcObject = ms;
      }
      const tryPlay = () => { v.play().catch(() => {}); };
      tryPlay();
      ev.track.onunmute = tryPlay;
      setStep("已收到屏幕画面", "done");
    };
    pc.onconnectionstatechange = () => {
      const st = pc.connectionState;
      $("stat").textContent = "连接状态：" + st + (currentName ? "（" + currentName + "）" : "");
      if (st === "failed" || st === "disconnected") {
        $("stat").textContent = "直连失败：" + st + "（可改用教室电脑上的隧道地址访问）";
      }
    };

    // 先声明一个"只收"的视频行：answer 不能新增 media 行，必须由 offer 发起
    try { pc.addTransceiver("video", { direction: "recvonly" }); } catch (e) {}
    dc = pc.createDataChannel("rpc", { ordered: true });
    dc.onopen = () => {
      for (const li of Array.from($("steps").children)) {
        if (li.textContent.indexOf("还没连上") >= 0) li.remove();
      }
      connectRetry = 0;
      if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
      setStep("直连已建立", "done");
      const saved = localStorage.getItem(rememberKey(currentSn));
      if (saved) {
        autoLogin(saved);
      } else {
        $("login-device").textContent = "已连上：" + currentName + "（" + currentSn + "）";
        show("view-login");
      }
    };
    dc.onmessage = (ev) => onRpc(ev.data);

    let wsWatchdog = setTimeout(() => {
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
    ws.onclose = () => { if (connectTimer) { /* 保持原逻辑 */ } };

    ws.onopen = async () => {
      if (wsWatchdog) { clearTimeout(wsWatchdog); wsWatchdog = null; }
      setStep("交换连接信息…");
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
    };

    // 15 秒没连上 → 给出兜底提示
    // 10 秒没连上就自动重试，最多 5 次；全失败才提示回落
    connectTimer = setTimeout(() => {
      const opened = dc && dc.readyState === "open";
      const pcOk = pc && (pc.connectionState === "connected" || pc.iceConnectionState === "connected"
                          || pc.iceConnectionState === "completed");
      if (opened || pcOk) return;               // 已连上就别重试，否则会把好连接拆掉
      const failed = pc && (pc.connectionState === "failed" || pc.iceConnectionState === "failed");
      if (!failed && connectRetry === 0) {
        // 第一次没到 20 秒就失败判定的，再多等一轮
      }
      const maxTry = offlineFast ? 2 : MAX_RETRY;
      if (connectRetry < maxTry) {
        connectRetry += 1;
        setStep("没连上，第 " + connectRetry + "/" + MAX_RETRY + " 次自动重试…");
        teardown(false);
        setTimeout(() => connect({ sn: currentSn, name: currentName }), 800);
      } else {
        setStep(offlineFast
          ? "这台电脑现在显示离线（没开机或没联网）——请确认后重试，或改用电脑上显示的隧道地址"
          : "试了 " + maxTry + " 次仍连不上：网络可能封了 UDP，请改用教室电脑上显示的隧道地址（兼容模式）");
        $("btn-cancel").textContent = "返回列表";
        show("view-fallback");
        $("fallback-msg").textContent = "当前机器：" + currentName + "（" + currentSn + "）";
      }
    }, offlineFast ? 8000 : 20000);
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
      if (msg.role === "web") return;   // 别的老师进来了，与我无关
      setStep("电脑已在线");
      // 之前 offer 发早了（电脑还没进房间）→ 现在重新协商
      if (pc && dc && dc.readyState !== "open") {
        try {
          const pcJoined = await pc.createOffer();
          await pc.setLocalDescription(pcJoined);
          ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
          setStep("已重新发起连接…");
        } catch (e) { /* 忽略 */ }
      }
      return;
    }
    if (msg.type === "peer-left") { setStep("电脑断开了"); return; }
    if (msg.peerId && myId && msg.peerId !== myId) return;   // 不是发给我的
    if (msg.type === "answer" && pc) {
      await pc.setRemoteDescription(msg.sdp);
      setStep("直连建立中…");
      return;
    }
    if (msg.type === "ice" && pc && msg.candidate) {
      try { await pc.addIceCandidate(msg.candidate); } catch (e) { /* 忽略 */ }
    }
  }


  /* ---------------- 手机远控：点屏幕 = 电脑上点一下 ---------------- */
  let remoteOn = false, screenSize = null;

  async function toggleRemote() {
    remoteOn = !remoteOn;
    try {
      if (remoteOn) {
        await rpc("remote.start", {});
        const r = await rpc("remote.size", {});
        screenSize = { w: (r && (r.width || r.w)) || 1920, h: (r && (r.height || r.h)) || 1080 };
        $("btn-remote").textContent = "关闭远控";
        $("stat").textContent = "远控已开启（点画面即可操作）";
      } else {
        await rpc("remote.stop", {});
        $("btn-remote").textContent = "远控";
        $("stat").textContent = "远控已关闭";
      }
    } catch (e) {
      remoteOn = false;
      $("btn-remote").textContent = "远控";
      $("stat").textContent = "远控失败：" + e.message;
    }
  }

  function videoPoint(ev) {
    const v = $("screen");
    const r = v.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width;
    const py = (ev.clientY - r.top) / r.height;
    const w = (screenSize && screenSize.w) || 1920;
    const h = (screenSize && screenSize.h) || 1080;
    return { x: Math.round(px * w), y: Math.round(py * h) };
  }

  $("screen").addEventListener("click", async (ev) => {
    if (!remoteOn) return;
    const p = videoPoint(ev);
    try {
      await rpc("remote.input", { action: "absmove", x: p.x, y: p.y });
      await rpc("remote.input", { action: "click", button: "left" });
    } catch (e) { $("stat").textContent = "远控点击失败：" + e.message; }
  });

  $("screen").addEventListener("wheel", async (ev) => {
    if (!remoteOn) return;
    ev.preventDefault();
    try { await rpc("remote.input", { action: "scroll", delta: ev.deltaY > 0 ? -3 : 3 }); }
    catch (e) { /* 忽略 */ }
  }, { passive: false });



  /* ---------------- Service Worker 代理：所有 HTTP 都走 WebRTC ---------------- */
  let swSid = "";
  let swPort = null;

  async function ensureSW() {
    if (!("serviceWorker" in navigator)) return false;
    try {
      const reg = await navigator.serviceWorker.register("/p2p-sw.js", { scope: "/" });
      await navigator.serviceWorker.ready;
      swSid = Math.random().toString(36).slice(2, 10);
      const ch = new MessageChannel();
      swPort = ch.port1;
      swPort.onmessage = async (ev) => {
        const d = ev.data || {};
        const r = await window.cqtHttp(d.method || "GET", d.path || "/", d.body || null);
        swPort.postMessage({ replyTo: d.id, body: r });
      };
      swPort.start && swPort.start();
      (navigator.serviceWorker.controller || reg.active).postMessage(
        { type: "register-port", sid: swSid }, [ch.port2]);
      return true;
    } catch (e) { return false; }
  }

  /* ---------------- 复用原站界面 ---------------- */
  const APP_BASE = "https://call.xhzx.qzz.io/";   // 静态资源从这里取

  window.cqtHttp = async function (method, path, body) {
    try {
      if (method !== "GET") return await rpc("http.post", { path, body: body || {} });
      // 大响应分块拉取（DataChannel 单条消息有上限）
      let first = await rpc("http.get", { path, offset: 0, maxBytes: 30000 });
      if (!first || !first.more) return first;
      let chunks = [first.b64 || first.text || ""];
      let offset = first.offset + first.got;
      let guard = 0;
      while (first.more && guard++ < 200) {
        const next = await rpc("http.get", { path, offset, maxBytes: 30000 });
        chunks.push(next.b64 || next.text || "");
        first = Object.assign({}, first, { more: next.more });
        offset = next.offset + next.got;
        if (!next.got) break;
      }
      const joined = chunks.join("");
      return Object.assign({}, first, { more: false,
        b64: first.b64 != null ? joined : undefined,
        text: first.text != null ? joined : undefined });
    } catch (e) {
      return { status: 500, type: "text/plain", text: String(e.message || e) };
    }
  };

  const SHIM = `
(function () {
  const rawFetch = window.fetch ? window.fetch.bind(window) : null;
  function build(body) {
    const data = body.b64 ? Uint8Array.from(atob(body.b64), c => c.charCodeAt(0))
                          : (body.text != null ? body.text : "");
    return new Response(data, { status: body.status || 200,
      headers: { "Content-Type": body.type || "text/plain; charset=utf-8" } });
  }
  window.__shimHits = [];
  window.fetch = function (url, opts) {
    opts = opts || {};
    if (typeof url === "string" && url.charAt(0) === "/") {
      let payload = null;
      if (opts.body) { try { payload = JSON.parse(opts.body); } catch (e) { payload = null; } }
      const m = (opts.method || "GET").toUpperCase();
      if (!parent || typeof parent.cqtHttp !== "function") {
        window.__shimHits.push("no-bridge:" + url);
        return rawFetch ? rawFetch(url, opts) : Promise.reject(new Error("no bridge"));
      }
      window.__shimHits.push(m + " " + url);
      return parent.cqtHttp(m, url, payload).then((b) => {
        window.__shimHits.push("<- " + url + " " + (b && b.status));
        return build(b);
      }).catch((e) => {
        window.__shimHits.push("!! " + url + " " + e.message);
        throw e;
      });
    }
    return rawFetch ? rawFetch(url, opts) : Promise.reject(new Error("no fetch"));
  };
  const OXHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function () {
    const x = new OXHR();
    const oOpen = x.open.bind(x), oSend = x.send.bind(x);
    let m = "GET", u = "";
    x.open = function (method, url) { m = method; u = url; return oOpen.apply(null, arguments); };
    x.send = function (body) {
      if (!u || u.charAt(0) !== "/") return oSend(body);
      let payload = null;
      try { payload = body ? JSON.parse(body) : null; } catch (e) { payload = null; }
      parent.cqtHttp(m, u, payload).then(function (r) {
        const text = r.text != null ? r.text : "";
        const defs = { readyState: 4, status: r.status || 200, responseText: text, response: text };
        for (const k in defs) Object.defineProperty(x, k, { value: defs[k], configurable: true });
        x.dispatchEvent(new Event("readystatechange"));
        x.dispatchEvent(new Event("load"));
        if (x.onload) x.onload();
      }).catch(function (err) {
        x.dispatchEvent(new Event("error"));
        if (x.onerror) x.onerror(err);
      });
    };
    return x;
  };
})();
`;

  async function loadOriginalApp(path) {
    path = path || "/";
    $("stat").textContent = "正在载入界面…";
    let r = null;
    for (let i = 0; i < 8; i++) {
      r = await window.cqtHttp("GET", path);
      if (r && (r.status === 200 || r.status === 304) && (r.text || "").length > 200) break;
      await new Promise((res) => setTimeout(res, 700));
    }
    if (!r || (r.status !== 200 && r.status !== 304)) {
      $("stat").textContent = "载入界面失败：HTTP " + (r && r.status);
      return;
    }
    let html = r.text || "";
    let swOk = false, swErr = "";
    // 默认走"静态资源 CDN + 接口全走 DataChannel"的稳定路径；
    // 想要"连静态资源也走 WebRTC"可加 ?p2p=sw（实验特性）
    const wantSW = new URLSearchParams(location.search).get("p2p") === "sw";
    if (wantSW) {
      try { swOk = await ensureSW(); } catch (e) { swErr = String((e && e.message) || e); }
    }
    if (swOk && swSid) {
      // 所有绝对路径改走 /p2p/<sid>/*，由 Service Worker 转成 DataChannel 请求
      html = html.replace(/(\s(?:src|href|action)=)(["'])\/(?!p2p\/)/g,
                          "$1$2/p2p/" + swSid + "/");
    } else {
      // 退回：静态资源走 CDN，接口由 fetch/XHR 垫片桥接
      html = html.replace(/(["'(])\/static\//g, "$1app-static/");
    }
    const head = '<base href="' + APP_BASE + '"><script>' + SHIM + "<\/script>";
    html = html.replace(/<head([^>]*)>/i, "<head$1>" + head);
    html = html.replace("</body>", "<script>document.addEventListener('DOMContentLoaded',"
      + "function(){document.body.classList.remove('drawer-open');});<\/script></body>");
    $("app-frame").srcdoc = html;
    $("iframe-title").textContent = currentName + "（" + currentSn + "）";
    show("view-iframe");
    $("stat").textContent = "已连接：" + currentName + "（" + currentSn + "）· 直连模式";
  }

  /* ---------------- RPC ---------------- */
  function rpc(method, params) {
    return new Promise((resolve, reject) => {
      if (!dc || dc.readyState !== "open") { reject(new Error("还没连上电脑")); return; }
      const id = ++rpcSeq;
      pending.set(id, { resolve, reject });
      dc.send(JSON.stringify({ id, method, params: params || {}, token }));
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); reject(new Error("超时")); }
      }, offlineFast ? 8000 : 20000);
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
      $("call-result").textContent = "已叫号：" + (msg.data && msg.data.phrase ? msg.data.phrase : "");
      speech(msg.data && msg.data.phrase);
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
      $("app-title").textContent = currentName;
      $("stat").textContent = "已连接：" + currentName + "（" + currentSn + "）";
      show("view-app");
      loadOriginalApp("/");
    } catch (e) {
      err(e.message === "超时" ? "登录超时，请重试" : e.message);
      localStorage.removeItem(rememberKey(currentSn));
      $("login-device").textContent = "已连上：" + currentName + "（" + currentSn + "）";
      show("view-login");
    } finally {
      $("btn-login").disabled = false;
    }
  }

  async function autoLogin(savedJson) {
    try {
      const saved = JSON.parse(savedJson);
      show("view-login");
      $("login-device").textContent = "正在用这台设备记住的账号自动登录…";
      await doLogin(saved);
    } catch (e) {
      show("view-login");
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  $("btn-refresh").addEventListener("click", loadMachines);
  $("btn-manage").addEventListener("click", () => {
    show("view-manage");
    masterPwd = "";
    $("manage-lock").classList.remove("hidden");
    $("manage-body").classList.add("hidden");
    $("in-master").value = "";
    $("manage-err").textContent = "";
    loadMachines().then(renderManage);
    setTimeout(() => { const el = $("in-master"); if (el) el.focus(); }, 100);
  });
  $("btn-manage-close").addEventListener("click", () => {
    masterPwd = "";
    $("in-master").value = "";
    show("view-list");
  });
  $("btn-unlock").addEventListener("click", manageUnlock);
  $("in-master").addEventListener("keydown", (e) => { if (e.key === "Enter") manageUnlock(); });
  $("btn-create").addEventListener("click", manageCreate);
  $("btn-login").addEventListener("click", () => doLogin(null));
  $("btn-cancel").addEventListener("click", () => { teardown(); show("view-list"); });
  $("btn-open-fallback").addEventListener("click", () => {
    const u = ($("in-fallback-url") || {}).value.trim();
    if (!/^https?:\/\//i.test(u)) { $("fallback-msg").textContent = "请填完整的 https 地址"; return; }
    window.open(u, "_blank");
  });
  $("btn-retry-connect").addEventListener("click", () => {
    connectRetry = 0;
    connect({ sn: currentSn, name: currentName });
  });
  $("btn-back-list2").addEventListener("click", () => { teardown(); show("view-list"); loadMachines(); });
  $("btn-exit").addEventListener("click", () => {
    localStorage.removeItem(rememberKey(currentSn));
    teardown();
    show("view-list");
    loadMachines();
  });
  $("btn-call").addEventListener("click", async () => {
    const number = $("in-number").value.trim();
    if (!number) { $("call-result").textContent = "请先填学号"; return; }
    try {
      const r = await rpc("call", { number, note: $("in-note").value.trim() });
      $("call-result").textContent = r && r.phrase ? r.phrase : "已叫号";
      speech(r && r.phrase);
    } catch (e) { $("call-result").textContent = "失败：" + e.message; }
  });
  $("btn-remote").addEventListener("click", toggleRemote);
  $("btn-screen").addEventListener("click", async () => {
    screenWanted = !screenWanted;
    $("btn-screen").textContent = screenWanted ? "停止" : "开始";
    try { await rpc("screen", { on: screenWanted }); }
    catch (e) { $("stat").textContent = "屏幕开启失败：" + e.message; }
  });
  $("in-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(null); });
  $("btn-reload-app").addEventListener("click", () => loadOriginalApp("/"));
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  on("btn-goto-remote", () => show("view-app"));
  on("btn-back-iframe", () => show("view-iframe"));
  $("btn-back-list").addEventListener("click", () => { teardown(); show("view-list"); loadMachines(); });

  function teardown(resetView = true) {
    if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
    try { if (dc) dc.close(); } catch {}
    try { if (pc) pc.close(); } catch {}
    try { if (ws) ws.close(); } catch {}
    dc = null; pc = null; ws = null; token = ""; remoteOn = false;
    screenWanted = false;
    $("screen").srcObject = null;
    $("btn-screen").textContent = "开始";
    if (!resetView) return;
  }

  loadMachines();
  setInterval(() => { if (!$("view-list").classList.contains("hidden")) loadMachines(); }, 20000);
})();
