// 班级叫号器 P2P 门户 —— Cloudflare Worker
// 职责：机器注册（总密码校验）/ 心跳 / 机器列表 / Durable Object 信令房间 / ICE 配置
// 数据面（屏幕、远控、业务 RPC）全部走 WebRTC，Worker 只负责信令与机器清单。

const MAX_PEERS = 32;   // 一台电脑可同时被多位老师连接

const ICE_SERVERS = [
  { urls: ["stun:stun.miwifi.com:3478", "stun:stun.qq.com:3478"] },
  { urls: ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"] },
  { urls: ["stun:stun.cloudflare.com:3478"] },
];

function cors(resp) {
  const h = new Headers(resp.headers);
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  h.set("Access-Control-Allow-Headers", "Content-Type,Authorization");
  h.set("Cache-Control", "no-store");
  return new Response(resp.body, { status: resp.status, headers: h });
}

function json(obj, status = 200) {
  return cors(new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  }));
}

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function now() {
  return Math.floor(Date.now() / 1000);
}

// 常量时间比较，避免总密码被时序侧信道猜
function safeEqual(a, b) {
  const x = String(a || "");
  const y = String(b || "");
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

async function body(req) {
  try {
    return await req.json();
  } catch {
    return {};
  }
}

// ---------------- 机器注册 / 心跳 / 列表 ----------------

async function register(req, env) {
  const d = await body(req);
  const master = String(d.master || "");
  if (!safeEqual(master, env.MASTER_PWD || "")) {
    return json({ ok: false, error: "叫号器总密码不正确" }, 401);
  }
  const sn = String(d.sn || "").trim().toUpperCase().slice(0, 40);
  const name = String(d.name || "").trim().slice(0, 40) || sn;
  const version = String(d.version || "").slice(0, 20);
  const note = String(d.note || "").slice(0, 80);
  if (!sn) return json({ ok: false, error: "缺少机器 SN" }, 400);

  const secret = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "");
  const hash = await sha256(secret);
  const t = now();
  await env.DB.prepare(
    `INSERT INTO machines (sn, name, secret_hash, note, version, online, last_seen, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
     ON CONFLICT(sn) DO UPDATE SET
       name = excluded.name, secret_hash = excluded.secret_hash, note = excluded.note,
       version = excluded.version, online = 1, last_seen = excluded.last_seen,
       updated_at = excluded.updated_at`
  ).bind(sn, name, hash, note, version, t, t, t).run();

  await env.DB.prepare(
    "INSERT INTO audit (at, action, sn, ip, detail) VALUES (?, 'register', ?, ?, ?)"
  ).bind(t, sn, req.headers.get("CF-Connecting-IP") || "", name).run();

  return json({ ok: true, sn, name, secret, room: sn });
}

async function heartbeat(req, env) {
  const d = await body(req);
  const sn = String(d.sn || "").trim().toUpperCase();
  const secret = String(d.secret || "");
  if (!sn || !secret) return json({ ok: false, error: "缺少参数" }, 400);
  const row = await env.DB.prepare("SELECT secret_hash FROM machines WHERE sn = ?").bind(sn).first();
  if (!row) return json({ ok: false, error: "机器未注册" }, 404);
  if (!safeEqual(await sha256(secret), row.secret_hash)) {
    return json({ ok: false, error: "机器凭据无效，请重新注册" }, 401);
  }
  const t = now();
  await env.DB.prepare(
    "UPDATE machines SET online = 1, last_seen = ?, version = ?, updated_at = ? WHERE sn = ?"
  ).bind(t, String(d.version || "").slice(0, 20), t, sn).run();
  return json({ ok: true, t });
}

async function listMachines(req, env) {
  const t = now();
  const rows = await env.DB.prepare(
    "SELECT sn, name, note, version, last_seen FROM machines ORDER BY name COLLATE NOCASE"
  ).all();
  const items = (rows.results || []).map((r) => ({
    sn: r.sn,
    name: r.name,
    note: r.note || "",
    version: r.version || "",
    last_seen: r.last_seen || 0,
    online: r.last_seen && t - r.last_seen < 90, // 30s 心跳，>90s 判离线
  }));
  // 在线的排前面
  items.sort((a, b) => (b.online - a.online) || a.name.localeCompare(b.name));
  return json({ ok: true, now: t, machines: items });
}


async function verifyMaster(req, env) {
  const d = await body(req);
  if (!safeEqual(String(d.master || ""), env.MASTER_PWD || "")) {
    return json({ ok: false, error: "\u603b\u5bc6\u7801\u4e0d\u6b63\u786e" }, 401);
  }
  return json({ ok: true });
}

async function createMachine(req, env) {
  const d = await body(req);
  if (!safeEqual(String(d.master || ""), env.MASTER_PWD || "")) {
    return json({ ok: false, error: "\u603b\u5bc6\u7801\u4e0d\u6b63\u786e" }, 401);
  }
  const sn = String(d.sn || "").trim().toUpperCase().slice(0, 40);
  const name = String(d.name || "").trim().slice(0, 40) || sn;
  if (!sn) return json({ ok: false, error: "\u8bf7\u586b\u673a\u5668 SN" }, 400);
  const t = now();
  const row = await env.DB.prepare("SELECT sn FROM machines WHERE sn = ?").bind(sn).first();
  if (row) return json({ ok: false, error: "\u8fd9\u4e2a SN \u5df2\u7ecf\u5b58\u5728" }, 409);
  await env.DB.prepare(
    `INSERT INTO machines (sn, name, secret_hash, note, version, online, last_seen, created_at, updated_at)
     VALUES (?, ?, '', '', '', 0, 0, ?, ?)`
  ).bind(sn, name, t, t).run();
  await env.DB.prepare(
    "INSERT INTO audit (at, action, sn, ip, detail) VALUES (?, 'create', ?, ?, ?)"
  ).bind(t, sn, req.headers.get("CF-Connecting-IP") || "", name).run();
  return json({ ok: true, sn, name });
}

async function deleteMachine(req, env) {
  const d = await body(req);
  if (!safeEqual(String(d.master || ""), env.MASTER_PWD || "")) {
    return json({ ok: false, error: "总密码不正确" }, 401);
  }
  const sn = String(d.sn || "").trim().toUpperCase();
  await env.DB.prepare("DELETE FROM machines WHERE sn = ?").bind(sn).run();
  await env.DB.prepare(
    "INSERT INTO audit (at, action, sn, ip, detail) VALUES (?, 'delete', ?, ?, '')"
  ).bind(now(), sn, req.headers.get("CF-Connecting-IP") || "").run();
  return json({ ok: true });
}

async function renameMachine(req, env) {
  const d = await body(req);
  if (!safeEqual(String(d.master || ""), env.MASTER_PWD || "")) {
    return json({ ok: false, error: "总密码不正确" }, 401);
  }
  const sn = String(d.sn || "").trim().toUpperCase();
  const name = String(d.name || "").trim().slice(0, 40);
  if (!name) return json({ ok: false, error: "名字不能为空" }, 400);
  await env.DB.prepare("UPDATE machines SET name = ?, updated_at = ? WHERE sn = ?")
    .bind(name, now(), sn).run();
  return json({ ok: true });
}

// ---------------- 信令：Durable Object 房间 ----------------

export class SignalRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.meta = new Map(); // ws -> {role, sn}
  }

  metaFor(ws) {
    const cached = this.meta.get(ws);
    if (cached) return cached;
    for (const s of this.state.getWebSockets()) {
      if (s === ws) {
        const a = s.deserializeAttachment();
        if (a) return a;
      }
    }
    return null;
  }

  send(ws, obj) {
    try {
      ws.send(JSON.stringify(obj));
    } catch {}
  }

  broadcast(from, obj) {
    for (const s of this.state.getWebSockets()) {
      if (s !== from) this.send(s, obj);
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket", { status: 400 });
    }

    const role = url.searchParams.get("role") === "pc" ? "pc" : "web";
    const sn = (url.searchParams.get("sn") || "").toUpperCase();

    // 同一房间最多 1 台电脑 + N 个手机，这里限制电脑唯一
    if (role === "pc") {
      for (const s of this.state.getWebSockets()) {
        const m = this.metaFor(s);
        if (m && m.role === "pc") {
          try { s.close(4001, "pc reconnected"); } catch {}
        }
      }
    }

    const { 0: client, 1: server } = new WebSocketPair();
    this.state.acceptWebSocket(server);
    const ident = { role, sn, id: crypto.randomUUID().slice(0, 8) };
    server.serializeAttachment(ident);
    this.meta.set(server, ident);

    this.send(server, { type: "welcome", role, sn, you: ident.id,
                       peers: this.peerList(server) });
    this.broadcast(server, { type: "peer-joined", role, sn, id: ident.id,
                             peers: this.peerList() });
    return new Response(null, { status: 101, webSocket: client });
  }

  peerList(except) {
    const out = [];
    for (const s of this.state.getWebSockets()) {
      if (s === except) continue;
      const m = this.metaFor(s);
      if (m) out.push({ role: m.role, id: m.id });
    }
    return out;
  }

  async webSocketMessage(ws, raw) {
    let msg;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return;
    }
    const me = this.metaFor(ws);
    if (!me) return;

    if (msg.type === "ping") {
      this.send(ws, { type: "pong", t: Date.now() });
      return;
    }
    // 转发规则（支持一台电脑 + 多位老师并发）：
    //   * 老师发的 offer/ice → 只发给这台电脑（peerId 是老师自己的 id，电脑据此回包）
    //   * 电脑发的 offer/answer/ice → 只发给 peerId 指定的那位老师
    //   * 其它（bye / peer-left 等）→ 广播
    const out = { ...msg, from: me.role, fromId: me.id };
    const peerId = msg.peerId || "";

    if (me.role === "web") {
      for (const s of this.state.getWebSockets()) {
        const m = this.metaFor(s);
        if (m && m.role === "pc") this.send(s, out);
      }
      return;
    }
    if (peerId) {
      for (const s of this.state.getWebSockets()) {
        if (s === ws) continue;
        const m = this.metaFor(s);
        if (m && m.id === peerId) this.send(s, out);
      }
      return;
    }
    this.broadcast(ws, out);
  }

  async webSocketClose(ws) {
    const me = this.metaFor(ws);
    this.meta.delete(ws);
    this.broadcast(ws, { type: "peer-left", role: me ? me.role : "" });
  }

  async webSocketError(ws) {
    this.meta.delete(ws);
  }
}

// ---------------- 入口 ----------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));

    try {
      if (url.pathname === "/ice") return json({ iceServers: ICE_SERVERS });
      if (url.pathname === "/api/register" && request.method === "POST") {
        return register(request, env);
      }
      if (url.pathname === "/api/heartbeat" && request.method === "POST") {
        return heartbeat(request, env);
      }
      if (url.pathname === "/api/machines" && request.method === "GET") {
        return listMachines(request, env);
      }
      if (url.pathname === "/api/machines/verify" && request.method === "POST") {
        return verifyMaster(request, env);
      }
      if (url.pathname === "/api/machines/create" && request.method === "POST") {
        return createMachine(request, env);
      }
      if (url.pathname === "/api/machines/delete" && request.method === "POST") {
        return deleteMachine(request, env);
      }
      if (url.pathname === "/api/machines/rename" && request.method === "POST") {
        return renameMachine(request, env);
      }
      if (url.pathname === "/ws") {
        const sn = (url.searchParams.get("sn") || "default").toUpperCase();
        const room = env.ROOM.get(env.ROOM.idFromName(sn));
        return room.fetch(request);
      }
      if (url.pathname === "/" || url.pathname === "/health") {
        return json({ ok: true, service: "classcaller-portal", ts: now() });
      }
      return json({ ok: false, error: "not found" }, 404);
    } catch (exc) {
      return json({ ok: false, error: String((exc && exc.message) || exc) }, 500);
    }
  },
};
