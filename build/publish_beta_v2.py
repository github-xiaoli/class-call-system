# -*- coding: utf-8 -*-
"""发布【内测版】：源码推送 + 代码增量包 + 安装包 + release-info，只更新 channels.json 的 beta 通道。

用法：python build\\publish_beta_v2.py 2.0.29
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC_CODE = os.path.join(ROOT, "2.0.0")
REPO = "github-xiaoli/class-call-system"
TOKEN = open(r"C:\Users\李语晨\Desktop\github_api_key.txt", encoding="utf-8").read().strip()
OUTDIR = os.path.join(ROOT, "build", "rel_out_beta")

VERSION = sys.argv[1] if len(sys.argv) > 1 else "2.0.29"
TAG = "v%s-beta" % VERSION
ZIP_NAME = "classcaller-%s.zip" % VERSION
ASSET_EXE = "ClassCaller-Setup-v%s.exe" % VERSION
EXE = os.path.join(ROOT, "build", "out", "班级叫号器_安装包_v%s.exe" % VERSION)

NOTES = """- 【内测版】%(ver)s
- 图文说明书随代码一起下发：通过在线更新升级的电脑也能打开完整图文
- 托盘「📖 使用说明」打开软件内置说明书；安装包仍带 PDF（安装根目录）
- 说明书新增「十四、连不上的原因（环境限制）」（安卓/鸿蒙 .local、iPhone 白屏与装证书、校园网多播与隔离、DHCP 换 IP、防火墙等 25 条）
- mDNS 重写（根治"有时能解析有时不能"）：
  · 网卡枚举改用系统原生 GetAdaptersAddresses，不再依赖 PowerShell（学校禁 PowerShell 也能用）
  · 一个域名只广播一个 IP：不再把 Hyper-V / WSL / Wi-Fi Direct 等虚拟网卡地址混进 A 记录
  · 只绑定真实网卡收发；同机房多台电脑不再抢同一个服务名（撞名自动改名）
  · 新增网卡监听：换网 / 插网线 / Wi-Fi 连上 / IP 变化会自动重新注册
- 新增运行日志：/logs 实时查看 + 一键导出（写入 data/logs/，保留 7 天）；稳定版主页不显示入口，直接输网址可进
- mDNS 失败会提示：设置页红字写明原因，并附「mDNS 诊断 / 重新注册 / 查看日志」按钮
- 手机接入改用 IP：二维码、配对地址默认用 http(s)://电脑IP，安卓/鸿蒙也能打开（.local 只作备选）
- iOS 白屏根治：自动生成「本机根证书」并签发服务器证书（含 DNS 名字与局域网 IP），
  新增 /cert 引导页 + /cert.mobileconfig 描述文件 + /cert.crt，装一次并开启「完全信任」后所有浏览器都不白屏
- 新增排障入口：/plain（极简页）、/ping、/api/health（返回协议/服务器IP/客户端IP/UA）、主页白屏兜底提示
- 托盘新增「📖 使用说明」，直接打开软件内置的图文说明书（/manual）；安装包自带 PDF 与截图
- 图文使用说明新增「十四、连不上的原因（环境限制）」：安卓/鸿蒙不解析 .local、iPhone 白屏与装证书、
  校园 AP 多播过滤与客户端隔离、DHCP 换 IP、防火墙按网络类型生效等 25 条现场问题
- 向下兼容：旧版自签名证书启动时自动升级为"根证书 + 服务器证书"；旧配置/名单/记录无需迁移""" % {"ver": VERSION}


def log(m):
    print(m, flush=True)


def api(method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request("https://api.github.com" + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("User-Agent", "cqt-release")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8")) if body else None


def upload(release_id, path, name):
    url = ("https://uploads.github.com/repos/%s/releases/%d/assets?name=%s"
           % (REPO, release_id, urllib.parse.quote(name)))
    raw = open(path, "rb").read()
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("User-Agent", "cqt-release")
    req.add_header("Content-Type", "application/octet-stream")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=2400) as resp:
        r = json.loads(resp.read().decode("utf-8"))
    log("  上传 %-32s %7.1f MB  %.0fs" % (r["name"], r["size"] / 1048576.0, time.time() - t0))


def build_code_zip():
    os.makedirs(OUTDIR, exist_ok=True)
    zp = os.path.join(OUTDIR, ZIP_NAME)
    if os.path.isfile(zp):
        os.remove(zp)
    n = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(SRC_CODE):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, SRC_CODE)
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                arc = fn if rel == "." else os.path.join(rel, fn)
                zf.write(os.path.join(dirpath, fn), VERSION + "/" + arc.replace("\\", "/"))
                n += 1
    log("  代码包 %s：%d 个文件，%.2f MB" % (ZIP_NAME, n, os.path.getsize(zp) / 1048576.0))
    return zp


def env_meta():
    ch = json.load(io.open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))
    src = ch.get("dev") or ch.get("stable") or {}
    out = {k: src.get(k, "") for k in ("env_asset", "env_tag")}
    out["env_files"] = src.get("env_files", 0)
    out["env_bytes"] = src.get("env_bytes", 0)
    out["env_sha256"] = src.get("env_sha256", "")
    out["requires"] = src.get("requires", [])
    return out


def write_info(meta):
    info = {"version": VERSION, "channel": "beta", "notes": NOTES, "zip_asset": ZIP_NAME}
    info.update(meta)
    p = os.path.join(OUTDIR, "release-info.json")
    io.open(p, "w", encoding="utf-8", newline="\n").write(
        json.dumps(info, ensure_ascii=False, indent=2))
    return p


def release_body():
    base = "https://github.com/%s/releases/download/%s/" % (REPO, TAG)
    return """## 班级叫号器 %(ver)s（内测版）

⚠️ 这是**内测版**，改动较大（远控触摸/手势、扫码配对、向导记忆、全屏叫号等），请先小范围试用。

### 下载

| 文件 | 说明 |
| --- | --- |
| [%(exe_name)s](%(exe_url)s) | 向导式安装包（%(size).1f MB），覆盖安装不丢名单 |
| [%(zip_name)s](%(zip_url)s) | OTA 增量代码包（给已装好的软件自动更新用） |

### 本版更新

%(notes)s

### 许可

GNU GPL-3.0-or-later，全文见 [`LICENSE`](https://github.com/%(repo)s/blob/main/LICENSE)。
Copyright (C) 2026 xiaoli
""" % {
        "ver": VERSION, "exe_name": ASSET_EXE, "zip_name": ZIP_NAME,
        "exe_url": base + ASSET_EXE, "zip_url": base + ZIP_NAME,
        "size": os.path.getsize(EXE) / 1048576.0,
        "notes": NOTES, "repo": REPO,
    }


def push_source():
    git = ["git", "-C", ROOT]
    subprocess.run(git + ["add", "-A", "2.0.0", "channels.json", "README.md", "version.json"], check=True)
    msg = ("内测版 %s：远控触摸(多点API/批量帧/pointerId避让)、双指手势、扫码配对、"
           "向导记忆、全屏叫号、音频与证书修复" % VERSION)
    cfg = ["-c", "user.name=xiaoli", "-c", "user.email=xiaoli@users.noreply.github.com"]
    subprocess.run(git + cfg + ["commit", "-m", msg], check=True)
    url = "https://x-access-token:%s@github.com/%s.git" % (TOKEN, REPO)
    subprocess.run(git + ["push", url, "main:main"], check=True)
    log("  源码已推送到 main")


def purge_cdn():
    u = "https://purge.jsdelivr.net/gh/%s@main/channels.json" % REPO
    try:
        with urllib.request.urlopen(u, timeout=60) as r:
            log("  CDN 刷新 channels.json -> %s" % r.status)
    except Exception as exc:
        log("  CDN 刷新失败: %s" % exc)


def main():
    if not os.path.isfile(EXE):
        raise SystemExit("缺少安装包：" + EXE)

    log("1/5 生成代码包与 release-info")
    zip_path = build_code_zip()
    meta = env_meta()
    info_path = write_info(meta)

    log("2/5 创建/复用 release（内测版）")
    name = "班级叫号器 %s（内测版）" % VERSION
    try:
        rel = api("GET", "/repos/%s/releases/tags/%s" % (REPO, TAG))
        rid = rel["id"]
        log("  release 已存在 id=%s" % rid)
    except Exception:
        rel = api("POST", "/repos/%s/releases" % REPO, {
            "tag_name": TAG, "name": name, "body": release_body(),
            "prerelease": True, "draft": False})
        rid = rel["id"]
        log("  release 已创建 id=%s" % rid)

    log("3/5 更新 release 信息并上传资源")
    rel = api("PATCH", "/repos/%s/releases/%d" % (REPO, rid), {
        "name": name, "body": release_body(), "prerelease": True, "make_latest": False})
    keep = {ASSET_EXE, ZIP_NAME, "release-info.json"}
    for a in rel.get("assets", []):
        if a["name"] not in keep:
            api("DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]))
            log("  清理多余资源 " + a["name"])
    existing = {a["name"]: a for a in rel.get("assets", [])}
    for path, nm in ((zip_path, ZIP_NAME), (info_path, "release-info.json"), (EXE, ASSET_EXE)):
        if nm in existing:
            api("DELETE", "/repos/%s/releases/assets/%d" % (REPO, existing[nm]["id"]))
            log("  删除旧资源 " + nm)
        upload(rid, path, nm)

    log("4/5 只更新 channels.json 的 beta 通道")
    ch_path = os.path.join(ROOT, "channels.json")
    ch = json.load(io.open(ch_path, encoding="utf-8"))
    base = "https://github.com/%s/releases/download/%s/" % (REPO, TAG)
    entry = dict(ch.get("beta", {}))
    entry.update({
        "version": VERSION,
        "env_asset": meta["env_asset"], "env_tag": meta["env_tag"],
        "env_files": meta["env_files"], "env_bytes": meta["env_bytes"],
        "env_sha256": meta["env_sha256"], "requires": meta["requires"],
        "channel": "beta", "notes": NOTES,
        "zip_url": base + ZIP_NAME, "zip_asset": ZIP_NAME, "tag": TAG,
        "info_url": base + "release-info.json",
    })
    ch["beta"] = entry
    io.open(ch_path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(ch, ensure_ascii=False, indent=2))
    log("  beta -> %s（stable/dev 保持不变）" % VERSION)

    log("5/5 刷新 CDN（源码请用 build\\push_release.py 推送）")
    try:
        subprocess.run(["git", "-C", ROOT, "add", "-A", "2.0.0", "channels.json",
                        "README.md", "version.json", "docs", "build/guide"], check=False)
        subprocess.run(["git", "-C", ROOT, "-c", "user.name=xiaoli", "-c",
                        "user.email=xiaoli@users.noreply.github.com", "commit", "-m",
                        "内测版 %s" % VERSION], check=False)
        log("  已在本地提交（未执行 git push）")
    except Exception as exc:
        log("  本地提交跳过: %s" % exc)
    purge_cdn()
    log("完成：%s" % TAG)


main()
