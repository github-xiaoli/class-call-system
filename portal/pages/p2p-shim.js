/* P2P 连接宿主（页面内）
   —— 每个原站页面各自持有一条 WebRTC 连接；
   —— 页面之间用 BroadcastChannel 通报状态；
   —— 页面里的每一个 fetch(/api/...) 都改道到 DataChannel 上的 WebRPC，
      电脑那边执行的仍是原来的 Flask 接口，行为与本机完全一致。 */
(function () {
  if (window.__p2pShim) return;
  window.__p2pShim = true;

  const CFG = window.PORTAL_CONFIG || {};
  const API = CFG.apiBase || "";
  const WSB = CFG.wsBase || "";
  const $ = (id) => document.getElementById(id);

  let ws = null, pc = null, dc = null;
  let token = "";
  let sn = "";
  let myId = "";
  let state = "idle";
  let rpcSeq = 0;
  const pending = new Map();
  let connectRetry = 0;
  let connectTimer = null;
  const MAX_RETRY = 12;   // 网络抖动时多试几次
  let bc = null;

  try {
    bc = new BroadcastChannel("cc-p2p");
    bc.onmessage = (ev) => {
      const d = ev.data || {};
      if (d.type === "state") {
        window.__p2pState = d;
        document.dispatchEvent(new CustomEvent("p2pstate", { detail: d }));
      }
    };
  } catch (e) { }

  function setState(s) {
    state = s;
    window.__p2pState = { type: "state", state: s, sn, token: !!token };
    document.dispatchEvent(new CustomEvent("p2pstate", { detail: window.__p2pState }));
    if (bc) { try { bc.postMessage({ type: "state", state: s, sn, token: !!token }); } catch (e) {} }
  }

  async function connect(snArg, name) {
    sn = snArg;
    if (state === "connecting" || state === "connected") return;
    setState("connecting");
    try {
      let ice = { iceServers: [] };
      try {
        const r = await fetch(API + "/ice", { cache: "no-store" });
        ice = await r.json();
      } catch (e) { }
      if (!ice.iceServers || !ice.iceServers.length) {
        ice = { iceServers: [
          { urls: ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:443"] },
          { urls: ["stun:stun.l.google.com:19302"] },
        ] };
      }

      ws = new WebSocket(WSB + "/ws?sn=" + encodeURIComponent(sn) + "&role=web");
      await new Promise((res, rej) => {
        const t = setTimeout(() => rej(new Error("信令超时")), 40000);
        ws.onopen = () => { clearTimeout(t); res(); };
        ws.onerror = () => { clearTimeout(t); rej(new Error("信令连接失败")); };
      });
      ws.onmessage = (ev) => onSignal(ev);

      pc = new RTCPeerConnection({ iceServers: ice.iceServers || [] });
      pc.onicecandidate = (ev) => {
        if (ev.candidate && ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "ice", peerId: myId, candidate: ev.candidate }));
        }
      };
      pc.ontrack = (ev) => {
        window.__p2pStream = ev.streams && ev.streams[0];
        document.dispatchEvent(new CustomEvent("p2ptrack", { detail: window.__p2pStream }));
      };
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "connected") { connectRetry = 0; setState("connected"); }
        else if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
          setState("failed");
          scheduleRetry();
        }
      };
      try { pc.addTransceiver("video", { direction: "recvonly" }); } catch (e) {}

      dc = pc.createDataChannel("rpc", { ordered: true });
      dc.onopen = () => { setState("connected"); };
      dc.onmessage = (ev) => onRpc(ev.data);

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
    } catch (e) {
      window.__p2pError = String((e && e.message) || e);
      cleanup();
      setState("failed");
      scheduleRetry();
    }
  }

  function scheduleRetry() {
    if (connectTimer) return;
    if (connectRetry >= MAX_RETRY) return;
    connectRetry += 1;
    connectTimer = setTimeout(() => {
      connectTimer = null;
      connect(sn);
    }, 2000 * connectRetry);
  }

  function cleanup() {
    try { if (dc) dc.close(); } catch (e) {}
    try { if (pc) pc.close(); } catch (e) {}
    try { if (ws) ws.close(); } catch (e) {}
    dc = null; pc = null; ws = null;
  }

  function onSignal(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "welcome") { myId = msg.you || msg.fromId || ""; return; }
    if (msg.type === "peer-joined") {
      if (msg.role === "pc" && dc && dc.readyState !== "open") renegotiate();
      return;
    }
    if (msg.type === "peer-left") {
      if (msg.role === "pc" && (!msg.sn || msg.sn === sn)) { setState("failed"); scheduleRetry(); }
      return;
    }
    if (msg.peerId && myId && msg.peerId !== myId) return;
    if (msg.type === "answer" && pc) { pc.setRemoteDescription(msg.sdp).catch(() => {}); return; }
    if (msg.type === "ice" && pc && msg.candidate) {
      pc.addIceCandidate(msg.candidate).catch(() => {});
    }
  }

  async function renegotiate() {
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: "offer", peerId: myId, sdp: pc.localDescription }));
    } catch (e) {}
  }

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
    try { msg = JSON.parse(raw); } catch (e) { return; }
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.result);
    else p.reject(new Error(msg.error || "操作失败"));
  }

  window.p2p = {
    connect: connect,
    rawFetch: function (url, opts) { return nativeFetch(url, opts); },
    rpc: rpc,
    state: function () { return window.__p2pState || { state: "idle" }; },
    screen: function (on) { return rpc("screen", { on: !!on }); },
    hello: function () {},
    disconnect: function () { cleanup(); setState("idle"); },
  };

  const nativeFetch = window.fetch.bind(window);

  function isLocal(url) {
    try {
      const u = new URL(url, location.origin);
      return u.origin === location.origin && u.pathname.startsWith("/api/");
    } catch (e) { return false; }
  }
  function splitUrl(url) {
    const u = new URL(url, location.origin);
    const q = {};
    u.searchParams.forEach((v, k) => { q[k] = v; });
    return { path: u.pathname, query: q };
  }
  function makeResponse(data, status) {
    const text = JSON.stringify(data == null ? {} : data);
    return new Response(text, { status: status,
      headers: { "Content-Type": "application/json; charset=utf-8" } });
  }
  function fileToB64(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => {
        const s = String(fr.result || "");
        resolve(s.indexOf(",") >= 0 ? s.slice(s.indexOf(",") + 1) : s);
      };
      fr.onerror = () => reject(new Error("读取文件失败"));
      fr.readAsDataURL(file);
    });
  }

  window.fetch = async function (input, init) {
    const opts = init || {};
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (!isLocal(url)) return nativeFetch(input, opts);

    const { path, query } = splitUrl(url);
    const method = (opts.method || "GET").toUpperCase();

    if (state !== "connected") {
      if (path === "/api/auth/session") {
        return makeResponse({ ok: false, need_login: true, user: null }, 401);
      }
      return makeResponse({ ok: false, need_login: true,
        error: "还没连上电脑，请从首页选择教室电脑" }, 401);
    }

    if (method === "POST" && path === "/api/auth/login") {
      let body = {};
      try { body = JSON.parse(opts.body || "{}"); } catch (e) { body = {}; }
      try {
        const r = await rpc("auth.login", { username: body.username, password: body.password });
        token = r.token || "";
        return makeResponse({ ok: true, user: r.user || null, needs_change: false }, 200);
      } catch (err) {
        return makeResponse({ ok: false, error: String(err.message || err) }, 401);
      }
    }
    if (path === "/api/auth/session" && method === "GET") {
      if (!token) return makeResponse({ ok: false, need_login: true, user: null }, 401);
      try {
        const r = await rpc("auth.session", { token });
        return makeResponse({ ok: true, user: r.user, need_login: false }, 200);
      } catch (err) {
        token = "";
        return makeResponse({ ok: false, need_login: true, user: null }, 401);
      }
    }
    if (method === "POST" && path === "/api/auth/logout") {
      try { await rpc("auth.logout", { token: token }); } catch (e) {}
      token = "";
      return makeResponse({ ok: true }, 200);
    }

    if (opts.body instanceof FormData) {
      const fd = opts.body;
      const file = fd.get("file");
      const params = {};
      for (const [k, v] of fd.entries()) { if (k !== "file") params[k] = v; }
      const b64 = file ? await fileToB64(file) : "";
      try {
        const r = await rpc(method, { path: path, query: query, body: params,
          files: file ? { b64: b64, name: file.name, path: params.path || "" } : null, token: token });
        return makeResponse(r, 200);
      } catch (err) {
        return makeResponse({ ok: false, error: String(err.message || err) }, 500);
      }
    }

    let body = null;
    if (opts.body && typeof opts.body === "string") {
      try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; }
    }
    try {
      const r = await rpc(method, { path: path, query: query, body: body, token: token });
      return makeResponse(r, 200);
    } catch (err) {
      return makeResponse({ ok: false, error: String(err.message || err) }, 500);
    }
  };

  const params = new URLSearchParams(location.search);
  let snParam = params.get("sn");
  let nameParam = params.get("name");
  if (!snParam) {
    try { snParam = sessionStorage.getItem("cc-p2p-sn") || ""; } catch (e) {}
  }
  if (snParam) connect(snParam, nameParam || snParam);
})();
