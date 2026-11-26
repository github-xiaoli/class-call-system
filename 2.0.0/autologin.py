# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
r"""自动登录微软 Dev Tunnels 账号（用 GitHub 账号 + 密码，全程无人值守）。

为什么需要：微软不许导出"登录状态"，缓存令牌几周就失效，失效那天公网访问就断。
            所以让程序自己重新登录一次，老师完全不用管。

实测踩过的坑（都写在代码里了）：
  1. 页面正文里常出现 verify/captcha 字样，拿正文判断"要不要人机验证"会误判 ——
     只在"仍停在登录页"时才算失败；
  2. 设备码页前面还有一个 "Device Activation / Continue" 中间页要先点掉；
  3. 输码不是单个输入框，而是 8 个单字符框 user-code-0..8，第 4 格是那个横杠；
  4. 最后的 Authorize 按钮在 DOM 里是 disabled（看得见点不动），而 value="0"
     那个其实是 Cancel —— 必须只点 value="1"，并且先解禁。
"""
import json
import os
import re
import subprocess
import sys
import time

CODE_RE = re.compile(r"([A-Z0-9]{4}-[A-Z0-9]{4})")


# ---------------------------------------------------------------------------
# 凭据：Windows DPAPI 加密，只有这台电脑的这个用户能解
# ---------------------------------------------------------------------------
def _cred_path():
    base = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "ClassCaller")
    return os.path.join(base, "github_login.dat")


def save_credentials(username, password):
    try:
        import win32crypt
    except Exception as exc:
        return False, "缺少 pywin32，无法加密保存：%r" % (exc,)
    blob = json.dumps({"username": username, "password": password},
                      ensure_ascii=False).encode("utf-8")
    enc = win32crypt.CryptProtectData(blob, "ClassCallerAutoLogin", None, None,
                                      None, 0)
    p = _cred_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(enc)
    os.replace(tmp, p)
    return True, ""


def load_credentials():
    try:
        import win32crypt
        with open(_cred_path(), "rb") as f:
            enc = f.read()
        blob = win32crypt.CryptUnprotectData(enc, None, None, None, 0)[1]
        data = json.loads(blob.decode("utf-8"))
        if data.get("username") and data.get("password"):
            return data
    except Exception:
        pass
    return None


def has_credentials():
    return load_credentials() is not None


def clear_credentials():
    try:
        os.remove(_cred_path())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 设备码
# ---------------------------------------------------------------------------
def start_device_code(exe_path, env_fn, log):
    """起 devtunnel 登录进程，返回 (proc, code)。"""
    proc = subprocess.Popen([exe_path, "user", "login", "-g", "-d"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            env=env_fn(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    code = ""
    deadline = time.time() + 40
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
            continue
        m = CODE_RE.search(line)
        if m:
            code = m.group(1)
            break
    return proc, code


# ---------------------------------------------------------------------------
# 页面操作小工具
# ---------------------------------------------------------------------------
def _wait_for(br, expr, timeout=20, log=None, what=""):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if br.js("return !!(%s);" % expr, wait=0.3):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    if log and what:
        log("[自动登录] 等不到：%s" % what)
    return False


def run(exe_path, env_fn, log=print, headless=True, timeout=180, progress=None):
    """跑一整轮自动登录。返回 (ok, 说明)。"""
    progress = progress or (lambda *_: None)
    import edge_cdp

    cred = load_credentials()
    if not cred:
        return False, "本机还没保存 GitHub 账号密码"
    user, pwd = cred["username"], cred["password"]

    log("[自动登录] 启动设备码登录…")
    proc, code = start_device_code(exe_path, env_fn, log)
    if not code:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "没拿到设备码（登录进程没有输出）"
    log("[自动登录] 设备码 %s" % code)

    br = None
    try:
        progress("启动浏览器")
        br = edge_cdp.Browser(headless=headless, log=log)
        log("[自动登录] 打开 GitHub 登录页")
        progress("打开 GitHub 登录页", br)
        br.nav("https://github.com/login", wait=1.0)
        progress("已定位登录表单", br)
        _wait_for(br, "document.querySelector('#login_field')", 25, log, "登录输入框")

        # 1) 填账号密码并提交
        br.js("""
            const u = document.querySelector('#login_field');
            const p = document.querySelector('#password');
            if (!u || !p) return 'noform';
            u.value = %s; p.value = %s;
            u.dispatchEvent(new Event('input', {bubbles:true}));
            p.dispatchEvent(new Event('input', {bubbles:true}));
            const btn = document.querySelector('input[type=submit][name=commit]')
                     || document.querySelector('button[type=submit]');
            if (btn) btn.click(); else u.form.submit();
            return 'submitted';
        """ % (json.dumps(user), json.dumps(pwd)), wait=3.0)
        log("[自动登录] 已提交账号密码，当前页面：%s" % br.title())

        # 2) 只在"还停在登录页"时才判定失败
        if _wait_for(br, "document.querySelector('#login_field')", 6, None):
            err = ""
            try:
                err = br.js("""
                    const e = document.querySelector('#js-flash-container .flash-error, .flash-error');
                    return e ? e.innerText.trim() : '';
                """, wait=0) or ""
            except Exception:
                pass
            shot = br.shot(os.path.join(_log_dir(), "autologin_login_fail.png"))
            # 两步验证 / 邮箱验证码：这不是密码错，需要用户介入
        try:
            probe = br.js("""
                const t = (document.body ? document.body.innerText : "").toLowerCase();
                if (document.querySelector('#otp, input[name="otp"], [autocomplete="one-time-code"]')) return "NEED_2FA";
                if (t.indexOf("two-factor") >= 0 || t.indexOf("two factor") >= 0) return "NEED_2FA";
                if (t.indexOf("verify your email") >= 0 || t.indexOf("check your email") >= 0
                    || t.indexOf("verification code") >= 0) return "NEED_EMAIL";
                return "";
            """)
        except Exception:
            probe = ""
        probe = (probe or "").strip().strip('"')
        if probe == "NEED_2FA":
            return False, ("NEED_2FA|GitHub 账号开了两步验证（2FA）。请先在本机浏览器打开 "
                           "github.com 手动登录一次（勾选记住此设备），再回来点「保存并登录」。")
        if probe == "NEED_EMAIL":
            return False, ("NEED_EMAIL|GitHub 要求邮箱验证码。请先在本机浏览器打开 github.com "
                           "手动登录一次，按要求填邮箱验证码，之后再回来点「保存并登录」。")
        return False, "GitHub 登录失败：%s%s" % (
                (err or "账号或密码不对"), ("（截图：%s）" % shot) if shot else "")

        # 3) 设备码页（先过"用这个账号继续"的中间页）
        log("[自动登录] 打开设备码页面")
        progress("正在处理设备码授权", br)
        br.nav("https://github.com/login/device", wait=1.5)
        for i in range(3):
            if br.js("return !!document.querySelector('#user-code-0');", wait=0.3):
                break
            clicked = br.js("""
                const f = document.querySelector('form[action="/login/device/select_account"]');
                if (f) { const b = f.querySelector('input[type=submit], button'); if (b) { b.click(); return 'ok'; } }
                const btn = document.querySelector('button[type=submit], input[type=submit]');
                if (btn) { btn.click(); return 'submit'; }
                return 'none';
            """, wait=2.0)
            log("[自动登录] 中间页：%s" % clicked)
            if clicked == 'none':
                time.sleep(1.5)

        # 4) 8 个单字符框逐格填（第 4 格是横杠，跳过）
        raw = code.replace("-", "").upper()
        res = br.js("""
            const chars = %s;
            const idx = [0,1,2,3,5,6,7,8];
            for (let i = 0; i < idx.length; i++) {
                const el = document.querySelector('#user-code-' + idx[i]);
                if (!el) return 'missing:' + idx[i];
                el.value = chars[i];
                el.dispatchEvent(new Event('input', {bubbles:true}));
            }
            const btn = document.querySelector('input[name=commit], button[type=submit]');
            if (btn) { btn.click(); return 'submitted'; }
            return 'no_submit';
        """ % json.dumps([c for c in raw]), wait=3.0)
        log("[自动登录] 填码结果：%s" % res)

        # 5) 授权页：只点 value="1"，disabled 就解禁
        _wait_for(br, "document.querySelector('button[name=authorize]')", 25, log,
                  "授权页面")
        log("[自动登录] 当前页面：%s" % br.title())
        clicked = br.js("""
            const f = document.querySelector('form[action="/login/device/authorize"]');
            const b = (f || document).querySelector('button[name=authorize][value="1"], input[name=authorize][value="1"]');
            if (!b) return 'nobtn';
            b.disabled = false;
            b.click();
            return 'clicked';
        """, wait=3.0)
        log("[自动登录] 授权点击：%s" % clicked)
        time.sleep(2)
        log("[自动登录] 授权后页面：%s" % br.title())
    except Exception as exc:
        shot = ""
        try:
            if br:
                shot = br.shot(os.path.join(_log_dir(), "autologin_error.png"))
        except Exception:
            pass
        return False, "浏览器自动化出错：%r%s" % (exc, ("（截图：%s）" % shot) if shot else "")
    finally:
        try:
            if br:
                br.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # 6) 用 CLI 确认
    rc, out = dtcli_run(exe_path, env_fn, ["user", "show"])
    m = re.search(r"Logged in as (\S+)", out or "")
    if m:
        log("[自动登录] 成功：%s" % m.group(1))
        return True, m.group(1)
    return False, "浏览器流程走完，但 devtunnel 仍未登录"


def _log_dir():
    d = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     "ClassCaller", "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return os.environ.get("TEMP", ".")
    return d


def dtcli_run(exe_path, env_fn, args, timeout=60):
    try:
        p = subprocess.run([exe_path] + list(args), capture_output=True,
                           stdin=subprocess.DEVNULL, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=env_fn(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except Exception as exc:
        return -1, repr(exc)
