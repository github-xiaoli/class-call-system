# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
文件传输：浏览电脑全部文件、新建文件夹、移动/复制、重命名、删除、
拖拽或选择文件上传、下载电脑文件。
"""
import os
import shutil
import string
import time

from flask import Blueprint, jsonify, request, send_file

from auth import check_access

fs_bp = Blueprint("fs", __name__)

# 禁止操作的关键系统目录（防止误删系统）
_BLOCK_NAMES = {"windows", "program files", "program files (x86)",
                "programdata", "system volume information", "$recycle.bin",
                "recovery", "boot"}


def _norm(p):
    try:
        return os.path.normpath(os.path.abspath(str(p)))
    except Exception:
        return ""


def _safe_name(name):
    name = os.path.basename(str(name))
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "未命名"


def _is_blocked(p):
    p = _norm(p)
    if not p:
        return True
    # 盘符根目录
    if len(p) == 3 and p[1:] == ":\\":
        return True
    base = os.path.basename(p).lower()
    if base in _BLOCK_NAMES:
        return True
    return False


def _unique_dest(dest_dir, name):
    base, ext = os.path.splitext(name)
    cand = os.path.join(dest_dir, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(dest_dir, "%s - 副本(%d)%s" % (base, i, ext))
        i += 1
    return cand


def _drives():
    drives = []
    for d in string.ascii_uppercase:
        p = d + ":\\"
        if os.path.exists(p):
            drives.append({"path": p, "name": d + ":"})
    return drives


@fs_bp.route("/api/fs/drives")
def api_fs_drives():
    err = check_access()
    if err:
        return err
    return jsonify({"ok": True, "drives": _drives()})


@fs_bp.route("/api/fs/list")
def api_fs_list():
    err = check_access()
    if err:
        return err
    path = _norm(request.args.get("path", ""))
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "路径不存在"}), 400
    parent = os.path.dirname(path)
    if parent == path:
        parent = ""
    items = []
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                is_dir = os.path.isdir(full)
                items.append({
                    "name": name,
                    "path": full,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(st.st_mtime)),
                })
            except Exception:
                items.append({"name": name, "path": full,
                              "is_dir": os.path.isdir(full), "size": 0,
                              "mtime": ""})
    except Exception as exc:
        return jsonify({"ok": False, "error": "无法读取目录"}), 400
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return jsonify({"ok": True, "path": path, "parent": parent, "items": items})


@fs_bp.route("/api/fs/mkdir", methods=["POST"])
def api_fs_mkdir():
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    path = _norm(data.get("path", ""))
    name = _safe_name(data.get("name", ""))
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "目录不存在"}), 400
    new_path = os.path.join(path, name)
    if os.path.exists(new_path):
        return jsonify({"ok": False, "error": "已存在同名项目"}), 400
    try:
        os.makedirs(new_path)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": "创建失败"}), 400


@fs_bp.route("/api/fs/rename", methods=["POST"])
def api_fs_rename():
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    path = _norm(data.get("path", ""))
    new_name = _safe_name(data.get("new_name", ""))
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "路径不存在"}), 400
    if _is_blocked(path):
        return jsonify({"ok": False, "error": "系统目录不允许重命名"}), 400
    dst = os.path.join(os.path.dirname(path), new_name)
    if os.path.exists(dst):
        return jsonify({"ok": False, "error": "已存在同名项目"}), 400
    try:
        os.rename(path, dst)
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "error": "重命名失败"}), 400


@fs_bp.route("/api/fs/copy", methods=["POST"])
def api_fs_copy():
    return _copy_move(False)


@fs_bp.route("/api/fs/move", methods=["POST"])
def api_fs_move():
    return _copy_move(True)


def _copy_move(is_move):
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    src = _norm(data.get("path", ""))
    dest_dir = _norm(data.get("dest_dir", ""))
    if not src or not os.path.exists(src):
        return jsonify({"ok": False, "error": "源路径不存在"}), 400
    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"ok": False, "error": "目标目录不存在"}), 400
    if _is_blocked(src):
        return jsonify({"ok": False, "error": "系统目录不允许操作"}), 400
    if os.path.dirname(src) == dest_dir and not is_move:
        pass  # 同目录复制走副本命名
    dst = _unique_dest(dest_dir, os.path.basename(src))
    try:
        if is_move:
            shutil.move(src, dst)
        else:
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        return jsonify({"ok": True, "dest": dst})
    except Exception as exc:
        return jsonify({"ok": False, "error": "操作失败"}), 400


@fs_bp.route("/api/fs/delete", methods=["POST"])
def api_fs_delete():
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    deleted = 0
    skipped = []
    for p in paths:
        p = _norm(p)
        if not p or not os.path.exists(p):
            skipped.append(p)
            continue
        if _is_blocked(p):
            skipped.append(p)
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            deleted += 1
        except Exception:
            skipped.append(p)
    return jsonify({"ok": True, "deleted": deleted, "skipped": len(skipped)})


@fs_bp.route("/api/fs/download", methods=["GET", "POST"])
def api_fs_download():
    # 允许 POST 表单携带 token：部分浏览器对脚本点击的 <a download> 不放行，
    # 用真实表单提交能稳定触发下载（且是流式，不吃内存）。
    from auth import is_local, verify_token

    if not is_local():
        token = (request.args.get("token") or request.form.get("token")
                 or request.headers.get("X-Auth-Token") or "")
        if not (token and verify_token(token)):
            return jsonify({"ok": False, "error": "需要密码激活后才能使用该功能"}), 401
    raw_path = request.form.get("path") or request.args.get("path", "")
    path = _norm(raw_path)
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    try:
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path),
                         conditional=True)
    except Exception:
        return jsonify({"ok": False, "error": "下载失败"}), 400


@fs_bp.route("/api/fs/upload", methods=["POST"])
def api_fs_upload():
    err = check_access()
    if err:
        return err
    dest_dir = _norm(request.form.get("path", ""))
    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"ok": False, "error": "目标目录不存在"}), 400
    files = request.files.getlist("files")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    saved = 0
    failed = 0
    for f in files:
        name = _safe_name(f.filename)
        if not name:
            failed += 1
            continue
        try:
            dst = _unique_dest(dest_dir, name)
            f.save(dst)
            saved += 1
        except Exception:
            failed += 1
    return jsonify({"ok": True, "saved": saved, "failed": failed})
