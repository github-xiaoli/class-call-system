// P2P 服务代理 Service Worker
// 作用：把页面里所有发往 /p2p/<sid>/... 的请求，转交给页面（通过 DataChannel 打到教室电脑），
// 这样原来的每一个 HTTP 请求（fetch、XHR、img、form、脚本）都走 WebRTC，不再经过服务器。

const CLIENTS = new Map();   // sid -> MessagePort

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "register-port" && data.sid) {
    CLIENTS.set(data.sid, event.ports && event.ports[0]);
  } else if (data.type === "unregister" && data.sid) {
    CLIENTS.delete(data.sid);
  }
});

let seq = 0;

function relay(port, payload) {
  return new Promise((resolve) => {
    const id = ++seq;
    const t = setTimeout(() => resolve({ status: 504, type: "text/plain",
                                         text: "电脑没有响应（超时）" }), 25000);
    const onMsg = (ev) => {
      const d = ev.data || {};
      if (d.replyTo !== id) return;
      clearTimeout(t);
      port.removeEventListener("message", onMsg);
      resolve(d.body || { status: 502, type: "text/plain", text: "空响应" });
    };
    port.addEventListener("message", onMsg);
    port.start && port.start();
    port.postMessage({ id, method: payload.method, path: payload.path, body: payload.body });
  });
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const m = url.pathname.match(/^\/p2p\/([^/]+)(\/.*)$/);
  if (!m) return;                       // 非代理请求：交给普通网络
  const sid = m[1];
  const realPath = m[2] + (url.search || "");
  const port = CLIENTS.get(sid);
  if (!port) return;                    // 没有页面登记：原样放行（会 404）

  event.respondWith((async () => {
    let body = null;
    if (event.request.method !== "GET" && event.request.method !== "HEAD") {
      try { body = await event.request.json(); } catch (e) { body = null; }
    }
    const r = await relay(port, { method: event.request.method, path: realPath, body });
    let payload = r.b64 ? Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0))
                        : (r.text != null ? r.text : "");
    return new Response(payload, {
      status: r.status || 200,
      headers: { "Content-Type": r.type || "application/octet-stream",
                 "Cache-Control": "no-store" },
    });
  })());
});
