# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
账号与会话存储（公网化改造 · 第 1 步）

把原来的「一次性密码 + 设备绑定」换成「用户名 + 密码 + 会话」。

- 账号 / 会话 / 设备 / 审计 存在 data/app.db（SQLite，Python 内置，零依赖）
- 名单与叫号记录仍是 data/roster.json、data/calls.json（便于 U 盘导入导出）
- 密码哈希沿用 scrypt + 随机盐；兼容旧的裸 SHA-256 记录，首次登录成功后自动升级

设计约定：
* 用户名不区分大小写（COLLATE NOCASE），存库前统一 strip().lower()
* 对外一律不透露"是用户名不存在还是密码错"，避免账号枚举
* 所有写操作各自开连接，靠 SQLite 的 WAL + 事务保证并发安全（Flask 是多线程）
"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")

# ⚠️ 不内置任何默认账号/密码。
#    首个管理员由「首次运行向导」或本机管理页创建，密码由用户自己设，
#    建号时带 must_change_pwd（首次登录强制改密）。
#    历史分支曾内置 OYCOMO/OYCOMO，公网暴露时等于管理员权限公开，已移除。
DEFAULT_ADMIN_USER = ""          # 仅保留常量名以兼容旧调用，值为空表示"没有内置账号"

ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"
STATUS_ACTIVE = "active"
STATUS_BANNED = "banned"

SESSION_TTL = 30 * 86400          # 记住我：30 天
SESSION_SLIDING = 3600            # 每次活跃最多续期一次/小时

_db_path = os.path.join(DATA_DIR, "app.db")


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------
def set_db_path(path):
    """测试用：切换数据库文件位置。"""
    global _db_path
    _db_path = path


def db_path():
    return _db_path


def _connect():
    folder = os.path.dirname(_db_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 自愈：数据库文件存在但表缺失（被删库/切错路径/并发初始化）时自动建表，
    # 避免调用方拿到 "no such table" 这种难以定位的错误。
    try:
        conn.execute("SELECT 1 FROM users LIMIT 1")
    except sqlite3.OperationalError:
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        except Exception:
            pass
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pwd_hash        TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'teacher',
    status          TEXT NOT NULL DEFAULT 'active',
    must_change_pwd INTEGER NOT NULL DEFAULT 0,
    display_name    TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    last_login      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    sid         TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    ua          TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    expires     REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    jwk        TEXT NOT NULL DEFAULT '{}',
    ua         TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    user_id  TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    ip       TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def init_db(seed_admin=False):
    """建表。**不再自动创建任何默认管理员**（seed_admin 参数保留但不再使用）。

    首个管理员由 app 启动时判断 count_users()==0 后调用 ensure_admin() 创建，
    密码来自首次运行向导/本机管理页，绝不写死在代码里。
    """
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return False


def count_users():
    """账号总数；0 表示还没建管理员（首次运行）。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def first_admin():
    """第一个启用中的管理员账号（本机免登录时用它代表"本机主人"）。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE role = ? AND status = ?"
            " ORDER BY created_at LIMIT 1", (ROLE_ADMIN, STATUS_ACTIVE)).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def ensure_admin(username, password, display_name="管理员"):
    """创建首个管理员。密码由用户自己设定，所以不再要求强制改密。"""
    if count_users() > 0:
        return None, "已有账号，不能再用此接口创建初始管理员"
    return create_user(username, password, role=ROLE_ADMIN,
                       display_name=display_name or "管理员", must_change=False)


# ---------------------------------------------------------------------------
# 密码哈希（scrypt + 随机盐，兼容旧的无盐 SHA-256）
# ---------------------------------------------------------------------------
_PW_N = 2 ** 14
_PW_R = 8
_PW_P = 1
_PW_DKLEN = 32
_PW_MAXMEM = 64 * 1024 * 1024


def _b64e(b):
    return base64.b64encode(b).decode("ascii")


def _b64d(s):
    return base64.b64decode(str(s).encode("ascii"))


def hash_password(password):
    """scrypt$n$r$p$盐$哈希；scrypt 不可用时退回 PBKDF2。"""
    password = str(password or "")
    salt = secrets.token_bytes(16)
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_PW_N, r=_PW_R, p=_PW_P,
                            maxmem=_PW_MAXMEM, dklen=_PW_DKLEN)
        return "scrypt$%d$%d$%d$%s$%s" % (_PW_N, _PW_R, _PW_P,
                                          _b64e(salt), _b64e(dk))
    except Exception:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, 200000, dklen=_PW_DKLEN)
        return "pbkdf2$200000$%s$%s" % (_b64e(salt), _b64e(dk))


def needs_rehash(stored):
    stored = str(stored or "")
    return not (stored.startswith("scrypt$") or stored.startswith("pbkdf2$"))


def verify_password(stored, password):
    """校验；兼容旧的裸 SHA-256。任何异常都返回 False，不抛。"""
    stored = str(stored or "")
    password = str(password or "")
    try:
        if stored.startswith("scrypt$"):
            _, n, r, p, salt_b64, dk_b64 = stored.split("$", 5)
            dk = hashlib.scrypt(password.encode("utf-8"), salt=_b64d(salt_b64),
                                n=int(n), r=int(r), p=int(p),
                                maxmem=_PW_MAXMEM, dklen=len(_b64d(dk_b64)))
            return hmac.compare_digest(_b64e(dk), dk_b64)
        if stored.startswith("pbkdf2$"):
            _, iters, salt_b64, dk_b64 = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     _b64d(salt_b64), int(iters),
                                     dklen=len(_b64d(dk_b64)))
            return hmac.compare_digest(_b64e(dk), dk_b64)
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored, legacy)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 用户名与账号
# ---------------------------------------------------------------------------
RESERVED = {"root", "system", "guest", "administrator"}


def normalize_username(name):
    return str(name or "").strip().lower()


def validate_username(name):
    """返回 (规范化后的用户名, 错误信息)。"""
    u = normalize_username(name)
    if not u:
        return "", "请输入用户名"
    if len(u) < 2 or len(u) > 20:
        return "", "用户名需 2–20 个字符"
    if u in RESERVED:
        return "", "该用户名为系统保留"
    for ch in u:
        if not (ch.isalnum() or ch in "_-.@"):
            return "", "用户名只能包含字母、数字、下划线、点、横线"
    return u, None


def _row_to_user(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "status": row["status"],
        "must_change_pwd": bool(row["must_change_pwd"]),
        "display_name": row["display_name"] or row["username"],
        "created_at": row["created_at"],
        "last_login": row["last_login"],
        "is_admin": row["role"] == ROLE_ADMIN,
    }


def get_user(username):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (normalize_username(username),)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def get_user_by_id(uid):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def create_user(username, password, role=ROLE_TEACHER, display_name="",
                must_change=False):
    """返回 (user, 错误信息)。用户名重复时返回错误而不是抛异常。"""
    u, err = validate_username(username)
    if err:
        return None, err
    if len(str(password or "")) < 4:
        return None, "密码至少 4 位"
    if role not in (ROLE_ADMIN, ROLE_TEACHER):
        return None, "角色无效"
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (u,)).fetchone():
            return None, "该用户名已存在"
        uid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username, pwd_hash, role, status,"
            " must_change_pwd, display_name, created_at, last_login)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, u, hash_password(password), role, STATUS_ACTIVE,
             1 if must_change else 0, str(display_name or u), time.time(), 0))
        conn.commit()
        return get_user_by_id(uid), None
    finally:
        conn.close()


def list_users():
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY role DESC, created_at").fetchall()
        out = []
        for r in rows:
            u = _row_to_user(r)
            u["online"] = count_sessions(r["id"]) > 0
            out.append(u)
        return out
    finally:
        conn.close()


def set_password(uid, password, must_change=False):
    if len(str(password or "")) < 4:
        return False, "密码至少 4 位"
    conn = _connect()
    try:
        conn.execute("UPDATE users SET pwd_hash = ?, must_change_pwd = ? WHERE id = ?",
                     (hash_password(password), 1 if must_change else 0, uid))
        conn.commit()
        return True, None
    finally:
        conn.close()


def set_status(uid, status):
    if status not in (STATUS_ACTIVE, STATUS_BANNED):
        return False
    conn = _connect()
    try:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, uid))
        conn.commit()
        if status == STATUS_BANNED:
            # 封禁要同时踢掉会话与设备，否则"踢了还能用"
            conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (uid,))
            conn.execute("UPDATE devices SET revoked = 1 WHERE user_id = ?", (uid,))
            conn.commit()
        return True
    finally:
        conn.close()


def delete_user(uid):
    conn = _connect()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return False, "账号不存在"
        if row["role"] == ROLE_ADMIN:
            n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = ?",
                             (ROLE_ADMIN,)).fetchone()["n"]
            if int(n) <= 1:
                return False, "至少要保留一个管理员"
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM devices WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def verify_login(username, password):
    """返回 (user, 错误信息)。不区分"用户不存在"与"密码错误"。"""
    user = get_user(username)
    if user is None:
        return None, "用户名或密码错误"
    conn = _connect()
    try:
        row = conn.execute("SELECT pwd_hash FROM users WHERE id = ?",
                           (user["id"],)).fetchone()
    finally:
        conn.close()
    if not row or not verify_password(row["pwd_hash"], password):
        return None, "用户名或密码错误"
    if user["status"] == STATUS_BANNED:
        return None, "账号已被封禁，请联系管理员"
    # 旧哈希在被成功使用后顺手升级
    if needs_rehash(row["pwd_hash"]):
        set_password(user["id"], password, must_change=user["must_change_pwd"])
    conn = _connect()
    try:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                     (time.time(), user["id"]))
        conn.commit()
    finally:
        conn.close()
    return user, None


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def create_session(user_id, ua="", ip="", ttl=SESSION_TTL):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (sid, user_id, ua, ip, created_at, last_seen,"
            " expires, revoked) VALUES (?,?,?,?,?,?,?,0)",
            (sid, user_id, str(ua or "")[:300], str(ip or "")[:64],
             now, now, now + int(ttl)))
        conn.commit()
    finally:
        conn.close()
    return sid


def get_session(sid):
    """返回 (session, user)；无效/过期/被封禁都返回 (None, None)。"""
    if not sid:
        return None, None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
        if not row:
            return None, None
        if int(row["revoked"]):
            return None, None
        if float(row["expires"]) < time.time():
            return None, None
        user = get_user_by_id(row["user_id"])
        if user is None or user["status"] == STATUS_BANNED:
            return None, None
        sess = {"sid": row["sid"], "user_id": row["user_id"], "ua": row["ua"],
                "ip": row["ip"], "created_at": row["created_at"],
                "last_seen": row["last_seen"], "expires": row["expires"]}
        return sess, user
    finally:
        conn.close()


def touch_session(sid, extend=True):
    """心跳：更新 last_seen；滑动续期（每小时最多一次）。"""
    conn = _connect()
    try:
        row = conn.execute("SELECT last_seen, expires FROM sessions WHERE sid = ?",
                           (sid,)).fetchone()
        if not row:
            return
        now = time.time()
        last = float(row["last_seen"] or 0)
        if extend and now - last > SESSION_SLIDING:
            conn.execute("UPDATE sessions SET last_seen = ?, expires = ? WHERE sid = ?",
                         (now, now + SESSION_TTL, sid))
        else:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE sid = ?", (now, sid))
        conn.commit()
    finally:
        conn.close()


def revoke_session(sid):
    conn = _connect()
    try:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE sid = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


def revoke_user_sessions(user_id, keep_sid=None):
    conn = _connect()
    try:
        if keep_sid:
            conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ? AND sid <> ?",
                         (user_id, keep_sid))
        else:
            conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def count_sessions(user_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND revoked = 0"
            " AND expires > ?", (user_id, time.time())).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def list_sessions():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT s.*, u.username, u.role FROM sessions s"
            " JOIN users u ON u.id = s.user_id"
            " WHERE s.revoked = 0 AND s.expires > ?"
            " ORDER BY s.last_seen DESC", (time.time(),)).fetchall()
        return [{"sid": r["sid"], "user_id": r["user_id"], "username": r["username"],
                 "role": r["role"], "ua": r["ua"], "ip": r["ip"],
                 "created_at": r["created_at"], "last_seen": r["last_seen"],
                 "expires": r["expires"]} for r in rows]
    finally:
        conn.close()


def purge_expired_sessions(older_than=7 * 86400):
    conn = _connect()
    try:
        cutoff = time.time() - older_than
        conn.execute("DELETE FROM sessions WHERE expires < ? OR (revoked = 1 AND last_seen < ?)",
                     (time.time(), cutoff))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 设备绑定（沿用原来的 ECDSA 思路，只是归属到 user）
# ---------------------------------------------------------------------------
def bind_device(user_id, jwk, ua="", ip=""):
    device_id = uuid.uuid4().hex
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO devices (device_id, user_id, jwk, ua, ip, created_at,"
            " last_seen, revoked) VALUES (?,?,?,?,?,?,?,0)",
            (device_id, user_id, str(jwk or "{}"), str(ua or "")[:300],
             str(ip or "")[:64], now, now))
        conn.commit()
    finally:
        conn.close()
    return device_id


def get_device(device_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM devices WHERE device_id = ?",
                           (device_id,)).fetchone()
        if not row or int(row["revoked"]):
            return None
        return {"device_id": row["device_id"], "user_id": row["user_id"],
                "jwk": row["jwk"], "ua": row["ua"], "ip": row["ip"],
                "created_at": row["created_at"], "last_seen": row["last_seen"]}
    finally:
        conn.close()


def list_devices():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT d.*, u.username FROM devices d JOIN users u ON u.id = d.user_id"
            " WHERE d.revoked = 0 ORDER BY d.last_seen DESC").fetchall()
        return [{"device_id": r["device_id"], "user_id": r["user_id"],
                 "username": r["username"], "ua": r["ua"], "ip": r["ip"],
                 "created_at": r["created_at"], "last_seen": r["last_seen"]}
                for r in rows]
    finally:
        conn.close()


def revoke_device(device_id):
    conn = _connect()
    try:
        conn.execute("UPDATE devices SET revoked = 1 WHERE device_id = ?", (device_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def audit(action, user_id="", username="", target="", ip="", detail=""):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO audit (ts, user_id, username, action, target, ip, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (time.time(), str(user_id or ""), str(username or ""), str(action),
             str(target or ""), str(ip or ""), str(detail or "")))
        conn.commit()
    except Exception:
        pass          # 审计失败不能影响主流程
    finally:
        conn.close()


def list_audit(limit=100, offset=0, action=None):
    conn = _connect()
    try:
        sql = "SELECT * FROM audit"
        args = []
        if action:
            sql += " WHERE action = ?"
            args.append(action)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        rows = conn.execute(sql, args).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "user_id": r["user_id"],
                 "username": r["username"], "action": r["action"],
                 "target": r["target"], "ip": r["ip"], "detail": r["detail"]}
                for r in rows]
    finally:
        conn.close()


def count_audit(action=None):
    conn = _connect()
    try:
        if action:
            row = conn.execute("SELECT COUNT(*) AS n FROM audit WHERE action = ?",
                               (action,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()
        return int(row["n"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# meta（本机免登录令牌等）
# ---------------------------------------------------------------------------
def get_meta(key, default=""):
    conn = _connect()
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default
    finally:
        conn.close()


def set_meta(key, value):
    conn = _connect()
    try:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?)"
                     " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                     (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def local_token():
    """本机免登录用的令牌：首次取用时生成并落库。"""
    tok = get_meta("local_token", "")
    if not tok:
        tok = secrets.token_urlsafe(32)
        set_meta("local_token", tok)
    return tok


# ---------------------------------------------------------------------------
# 命令行：建号 / 列表（第 1 步的验收手段）
# ---------------------------------------------------------------------------
def _cli(argv):
    import argparse
    parser = argparse.ArgumentParser(description="班级叫号器账号管理")
    parser.add_argument("--db", default=None, help="数据库路径（默认 data/app.db）")
    parser.add_argument("--create", metavar="USERNAME", help="创建账号")
    parser.add_argument("--password", default="", help="账号密码")
    parser.add_argument("--role", default=ROLE_TEACHER, choices=[ROLE_ADMIN, ROLE_TEACHER])
    parser.add_argument("--display", default="", help="显示名")
    parser.add_argument("--list", action="store_true", help="列出账号")
    parser.add_argument("--reset", metavar="USERNAME", help="重置密码")
    parser.add_argument("--ban", metavar="USERNAME", help="封禁账号")
    parser.add_argument("--unban", metavar="USERNAME", help="解封账号")
    args = parser.parse_args(argv)

    if args.db:
        set_db_path(args.db)
    init_db()

    if args.list:
        print("%-16s %-8s %-8s %s" % ("用户名", "角色", "状态", "在线"))
        for u in list_users():
            print("%-16s %-8s %-8s %s" % (u["username"], u["role"], u["status"],
                                          "是" if u["online"] else ""))
        return 0
    if args.create:
        user, err = create_user(args.create, args.password or "1234",
                                role=args.role, display_name=args.display,
                                must_change=True)
        print("创建成功: %s (%s)" % (user["username"], user["role"]) if user else "失败: " + err)
        return 0 if user else 1
    if args.reset:
        user = get_user(args.reset)
        if not user:
            print("账号不存在")
            return 1
        ok, err = set_password(user["id"], args.password or "1234", must_change=True)
        print("已重置 %s 的密码" % user["username"] if ok else "失败: " + err)
        return 0 if ok else 1
    if args.ban or args.unban:
        name = args.ban or args.unban
        user = get_user(name)
        if not user:
            print("账号不存在")
            return 1
        ok = set_status(user["id"], STATUS_BANNED if args.ban else STATUS_ACTIVE)
        print(("已封禁 " if args.ban else "已解封 ") + user["username"] if ok else "失败")
        return 0 if ok else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
