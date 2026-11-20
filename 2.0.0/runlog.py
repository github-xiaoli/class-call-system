# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
"""运行日志：把 print / logging 的输出同时收进内存环形缓冲和日志文件。

用途：
    出问题时老师不用截黑窗口，浏览器打开 /logs 就能看实时日志、一键导出。

对外接口：
    install(log_dir)                  安装（替换 sys.stdout/sys.stderr + 挂 logging）
    snapshot(after=-1, limit=500, ...) 取环形缓冲（网页轮询用）
    export_text()                     导出文本（下载用）
    write(level, text)                主动写一条
"""

import collections
import io
import os
import sys
import threading
import time

MAX_LINES = 4000          # 内存里保留多少行（够回溯一次启动 + 一次排障）
MAX_LINE_LEN = 4000       # 单行最长字符数，防止某行把内存吃爆
KEEP_DAYS = 7             # 日志文件保留天数
ROTATE_SIZE = 8 * 1024 * 1024   # 单个日志文件超过 8MB 就换一个

_LOCK = threading.Lock()
_BUF = collections.deque(maxlen=MAX_LINES)
_SEQ = 0
_LOG_DIR = ""
_LOG_FILE = ""
_LOG_STREAM = None
_ORIG_OUT = None
_ORIG_ERR = None
_INSTALLED = False
_RUN_STARTED = time.time()

_ERROR_WORDS = ("错误", "失败", "异常", "无法", "崩溃", "error", "failed", "traceback",
                "exception", "拒绝")
_WARN_WORDS = ("警告", "注意", "跳过", "未启用", "不支持", "warn", "超时", "重试")


def _guess_level(text):
    low = text.lower()
    if any(w in low for w in _ERROR_WORDS):
        return "error"
    if any(w in low for w in _WARN_WORDS):
        return "warn"
    return "info"


def _log_path_for(day=None):
    day = day or time.strftime("%Y%m%d")
    return os.path.join(_LOG_DIR, "app-%s.log" % day)


def _open_file():
    """打开当天日志文件（跨天/超大小自动换）。"""
    global _LOG_FILE, _LOG_STREAM
    path = _log_path_for()
    if _LOG_STREAM is not None and _LOG_FILE == path:
        try:
            if _LOG_STREAM.tell() < ROTATE_SIZE:
                return _LOG_STREAM
        except Exception:
            pass
        try:
            _LOG_STREAM.close()
        except Exception:
            pass
        # 超大小：改名另存一个
        try:
            os.replace(path, path + ".%d" % int(time.time()))
        except Exception:
            pass
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        _LOG_STREAM = io.open(path, "a", encoding="utf-8", errors="replace", newline="")
        _LOG_FILE = path
        # 清理太旧的日志
        try:
            cutoff = time.time() - KEEP_DAYS * 86400
            for name in os.listdir(_LOG_DIR):
                if not name.startswith("app-") or not name.endswith(".log"):
                    continue
                fp = os.path.join(_LOG_DIR, name)
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
        except Exception:
            pass
    except Exception:
        _LOG_STREAM = None
    return _LOG_STREAM


def write(text, level=None):
    """写一条日志（网页「查看日志」和日志文件都能看到）。"""
    global _SEQ
    text = str(text or "")
    if len(text) > MAX_LINE_LEN:
        text = text[:MAX_LINE_LEN] + "…（已截断）"
    level = level or _guess_level(text)
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    full = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    with _LOCK:
        _SEQ += 1
        entry = {"seq": _SEQ, "t": stamp, "ts": now, "level": level, "text": text}
        _BUF.append(entry)
        try:
            f = _open_file()
            if f is not None:
                f.write("[%s] %s\n" % (full, text))
                f.flush()
        except Exception:
            pass
    return entry


class _Tee(object):
    """把 print 的输出同时送给原控制台、环形缓冲和日志文件。"""

    def __init__(self, orig, name):
        self.orig = orig
        self.name = name
        self._buf = ""

    # --- 文件类接口 ---
    def write(self, data):
        data = data if isinstance(data, str) else str(data)
        try:
            if self.orig is not None:
                self.orig.write(data)
        except Exception:
            pass
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                write(line.rstrip("\r"))
        return len(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            if self.orig is not None:
                self.orig.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self.orig and self.orig.isatty())
        except Exception:
            return False

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def fileno(self):
        if self.orig is None:
            raise io.UnsupportedOperation("fileno")
        return self.orig.fileno()

    def close(self):
        try:
            if self._buf.strip():
                write(self._buf.rstrip("\r\n"))
                self._buf = ""
        except Exception:
            pass


def install(log_dir):
    """装好日志收集。重复调用安全。"""
    global _LOG_DIR, _ORIG_OUT, _ORIG_ERR, _INSTALLED
    _LOG_DIR = log_dir
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    if _INSTALLED:
        return
    _INSTALLED = True
    _ORIG_OUT, _ORIG_ERR = sys.stdout, sys.stderr
    try:
        sys.stdout = _Tee(_ORIG_OUT, "stdout")
        sys.stderr = _Tee(_ORIG_ERR, "stderr")
    except Exception:
        pass
    # 让第三方库的 logging 也进缓冲区
    try:
        import logging

        class Handler(logging.Handler):
            def emit(self, record):
                try:
                    write(record.getMessage(),
                          level={logging.ERROR: "error", logging.WARNING: "warn",
                                 logging.CRITICAL: "error"}.get(record.levelno, "info"))
                except Exception:
                    pass

        h = Handler()
        h.setLevel(logging.INFO)
        root = logging.getLogger()
        if root.level > logging.INFO or root.level == 0:
            root.setLevel(logging.INFO)
        root.addHandler(h)
    except Exception:
        pass
    write("=" * 46)
    write("日志已启动（本次运行开始于 %s）"
          % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_RUN_STARTED)))


def snapshot(after=-1, limit=500, keyword="", level=""):
    """取日志：after 为已收到的最大序号，返回比它新的行。"""
    kw = (keyword or "").strip().lower()
    with _LOCK:
        items = list(_BUF)
    out = []
    for e in items:
        if e["seq"] <= after:
            continue
        if level and level != "all" and e["level"] != level:
            continue
        if kw and kw not in e["text"].lower():
            continue
        out.append(e)
    if limit and len(out) > int(limit):
        out = out[-int(limit):]
    next_seq = items[-1]["seq"] if items else after
    return {"lines": out, "next": next_seq, "kept": len(items),
            "total": _SEQ, "boot": _RUN_STARTED}


def export_text():
    """导出：今天的日志文件全文（含本次运行），取不到就退回内存缓冲。"""
    chunks = []
    try:
        path = _log_path_for()
        if os.path.isfile(path):
            with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                chunks.append(f.read())
    except Exception:
        pass
    if not chunks:
        with _LOCK:
            items = list(_BUF)
        chunks.append("\n".join("[%s] %s" % (time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e["text"]) for e in items))
    header = ("班级叫号器 运行日志\n"
              "导出时间：%s\n"
              "本次运行开始：%s\n"
              "%s\n\n" % (
                  time.strftime("%Y-%m-%d %H:%M:%S"),
                  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_RUN_STARTED)),
                  "-" * 40))
    return header + "".join(chunks)


def log_file_path():
    return _LOG_FILE or _log_path_for()
