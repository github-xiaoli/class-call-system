// 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
// SPDX-License-Identifier: GPL-3.0-or-later
//
// 统一登录后的前端鉴权辅助。
//
// 历史背景：原版用「一次性密码 + 浏览器 ECDSA 设备密钥」换 60 秒令牌，
// 用于在没有登录体系时保护敏感接口。现在改成账号会话（Cookie），
// 鉴权由服务端按会话完成，前端只需要：
//   1) 带上 Cookie（同源默认就会带）
//   2) 遇到 401 时跳登录页
//
// 之所以保留 Auth 这个对象和这些函数名，是为了不改动 files.js /
// monitor.js / remote.js / update.js 里几十处调用点。

const Auth = (() => {
  let _local = null;

  function loginUrl() {
    const next = encodeURIComponent(location.pathname + location.search);
    return "/login?next=" + next;
  }

  /** 未登录时跳转登录页（返回一个永不 resolve 的 Promise，阻断后续逻辑）。 */
  function gotoLogin() {
    location.href = loginUrl();
    return new Promise(() => {});
  }

  async function ensureLocal() {
    if (_local === null) {
      try {
        const m = await fetch("/api/auth/session").then((r) => r.json());
        _local = !!m.local;
      } catch (e) {
        _local = false;
      }
    }
    return _local;
  }

  /**
   * 把 URL 补成可直接用于 <img> / WebSocket 的地址。
   *
   * 会话鉴权靠 Cookie：同源请求浏览器自动携带，所以这里原样返回即可。
   * 保留这个函数是为了兼容 urlWithToken() 的调用点。
   */
  async function urlWithToken(path) {
    return path;
  }

  /** 带鉴权的 fetch：401 时跳登录页。 */
  async function fetchAuthed(url, options = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      ...options,
    });
    if (res.status === 401) {
      let needLogin = true;
      try {
        const d = await res.clone().json();
        needLogin = d.need_login !== false;
      } catch (e) { /* 非 JSON 响应，按未登录处理 */ }
      if (needLogin) return gotoLogin();
    }
    return res;
  }

  /** 兼容旧调用名。 */
  async function ensureToken() {
    const m = await fetch("/api/auth/session").then((r) => r.json())
      .catch(() => ({ logged_in: false }));
    if (!m.logged_in) return gotoLogin();
    return "";                       // 会话鉴权不需要令牌
  }

  async function me() {
    const d = await fetch("/api/auth/session").then((r) => r.json());
    return {
      ok: true,
      local: !!d.local,
      logged_in: !!d.logged_in,
      user: d.user || null,
    };
  }

  /** 退出登录。 */
  async function logout() {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "same-origin",
      });
    } catch (e) { /* 忽略网络错误 */ }
    location.href = "/login";
  }

  return {
    me,
    logout,
    ensureLocal,
    ensureToken,
    urlWithToken,
    fetchAuthed,
    gotoLogin,
  };
})();