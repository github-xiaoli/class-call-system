/* 密码激活 / 挑战签名 / 令牌管理
 * 使用浏览器原生 WebCrypto（ECDSA P-256）。
 * 私钥设为不可导出(extractable=false)，只以 CryptoKey 对象存 IndexedDB，
 * 任何代码都读不到私钥原文；换浏览器/清除数据后需重新激活。 */
"use strict";

const Auth = (() => {
  const DB_NAME = "cqt_auth_db";
  const STORE = "keys";
  const DEVICE_KEY = "cqt_device_id";
  let _token = null;
  let _tokenExp = 0;
  let _dbPromise = null;
  let _local = null;

  function openDb() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error("浏览器不支持 IndexedDB")); return; }
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => { req.result.createObjectStore(STORE); };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("IndexedDB 打开失败"));
    });
    return _dbPromise;
  }

  function idbPut(key, value) {
    return openDb().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    }));
  }
  function idbGet(key) {
    return openDb().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const rq = tx.objectStore(STORE).get(key);
      rq.onsuccess = () => resolve(rq.result || null);
      rq.onerror = () => reject(rq.error);
    }));
  }
  function idbDel(key) {
    return openDb().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    }));
  }

  function b64urlToBytes(s) {
    s = String(s).replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function bytesToB64url(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function requireSubtle() {
    if (!window.crypto || !crypto.subtle) {
      throw Object.assign(
        new Error("当前连接不是安全连接，浏览器加密不可用。请改用 https:// 安全地址访问后再激活。"),
        { code: "INSECURE" });
    }
  }

  async function hasPrivateKey() {
    try { return !!(await idbGet("privKey")); } catch (e) { return false; }
  }

  async function generateAndStoreKeyPair() {
    requireSubtle();
    // 私钥不可导出；仅把 CryptoKey 对象结构化克隆进 IndexedDB（依然读不到原文）
    const kp = await crypto.subtle.generateKey(
      { name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
    await idbPut("privKey", kp.privateKey);
    const pubJwk = await crypto.subtle.exportKey("jwk", kp.publicKey);
    return pubJwk;
  }

  async function activate(password) {
    const pubJwk = await generateAndStoreKeyPair();
    const res = await fetch("/api/auth/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, public_key: pubJwk }),
    });
    const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
    if (!res.ok || data.ok === false) {
      await idbDel("privKey");
      throw new Error(data.error || "激活失败");
    }
    localStorage.setItem(DEVICE_KEY, data.device_id);
    _token = null;
    return data;
  }

  async function sign(challengeB64url) {
    requireSubtle();
    const privKey = await idbGet("privKey");
    if (!privKey) {
      throw Object.assign(new Error("未激活，请先激活"), { code: "NEED_ACTIVATE" });
    }
    const sig = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" }, privKey, b64urlToBytes(challengeB64url));
    return bytesToB64url(new Uint8Array(sig));
  }

  async function ensureToken() {
    const did = localStorage.getItem(DEVICE_KEY);
    if (!did) {
      throw Object.assign(new Error("未激活，请先激活"), { code: "NEED_ACTIVATE" });
    }
    if (_token && Date.now() < _tokenExp - 5000) return _token;
    const res = await fetch("/api/auth/challenge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: did }),
    });
    const ch = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
    if (!res.ok || ch.ok === false) {
      if (/过期|撤销/.test(ch.error || "")) {
        localStorage.removeItem(DEVICE_KEY);
        await idbDel("privKey");
      }
      throw new Error(ch.error || "获取挑战失败");
    }
    const signature = await sign(ch.challenge);
    const vr = await fetch("/api/auth/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_id: ch.challenge_id, signature }),
    });
    const vdata = await vr.json().catch(() => ({ ok: false, error: "响应解析失败" }));
    if (!vr.ok || vdata.ok === false) {
      if (/过期|撤销/.test(vdata.error || "")) {
        localStorage.removeItem(DEVICE_KEY);
        await idbDel("privKey");
      }
      throw new Error(vdata.error || "签名校验失败");
    }
    _token = vdata.token;
    _tokenExp = Date.now() + (vdata.expires_in || 60) * 1000;
    return _token;
  }

  async function ensureLocal() {
    if (_local === null) {
      try {
        const m = await me();
        _local = !!m.local;
      } catch (e) {
        _local = false;
      }
    }
    return _local;
  }

  async function urlWithToken(path) {
    // 本机访问无需令牌
    if (await ensureLocal()) return path;
    const t = await ensureToken();
    return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
  }

  async function fetchAuthed(url, options = {}) {
    const doFetch = (tok) => fetch(url, {
      ...options,
      headers: { ...(options.headers || {}), ...(tok ? { "X-Auth-Token": tok } : {}) },
    });
    // 本机访问无需令牌，直接请求
    if (await ensureLocal()) return doFetch(null);
    let tok = await ensureToken();
    let res = await doFetch(tok);
    if (res.status === 401) {
      _token = null;
      tok = await ensureToken();
      res = await doFetch(tok);
    }
    return res;
  }

  async function deactivate() {
    const did = localStorage.getItem(DEVICE_KEY);
    if (did) {
      try {
        const tok = await ensureToken();
        await fetch("/api/auth/deactivate", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Auth-Token": tok },
          body: "{}",
        });
      } catch (e) { /* 服务器不可达时也照常清除本机密钥 */ }
    }
    localStorage.removeItem(DEVICE_KEY);
    await idbDel("privKey");
    _token = null;
  }

  async function me() {
    const did = localStorage.getItem(DEVICE_KEY) || "";
    try {
      const res = await fetch("/api/auth/me?device_id=" + encodeURIComponent(did));
      return await res.json();
    } catch (e) {
      return { ok: false, local: false, activated: false, has_valid_password: false };
    }
  }

  return {
    me, activate, deactivate, ensureToken, urlWithToken, fetchAuthed,
    hasPrivateKey, requireSubtle,
  };
})();
