# -*- coding: utf-8 -*-
"""向下兼容验收：旧版留下的「自签名叶子证书」在 2.0.30 上能自动升级成「根证书 + 服务器证书」。

场景：用户从 2.0.29 升级上来，data/ 里已经有旧的 ssl.crt / ssl.key（自签名、非 CA 签发）。
期望：启动后自动生成 ca.crt，并用它重签服务器证书；手机装 ca.crt 后 https 可用。

用软件自带 python 跑。
"""
import datetime as dt
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

WS = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC = os.path.join(WS, "2.0.0")
PY = os.path.join(WS, "build", "zip", "班级叫号器", "python", "python.exe")
WORK = os.path.join(WS, "_qa30_compat")
PORT, SPORT = 8790, 9790
results = []


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def make_old_cert(cert_path, key_path):
    """造一份 2.0.29 时期的证书：自签名叶子证书（没有根证书、issuer=subject）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oldstyle")])
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName("oldstyle"), x509.DNSName("oldstyle.local"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def wait_ok(url, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    root = os.path.join(WORK, "a")
    os.makedirs(os.path.join(root, "data"))
    shutil.copytree(SRC, os.path.join(root, "2.0.0"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(root, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "2.0.30", "data_dir": "data", "python_dir": "python"}, f)
    with open(os.path.join(root, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"port": PORT, "mdns_enabled": False, "require_admin": False,
                   "secure_enabled": True, "secure_port": SPORT,
                   "update_policy": "none", "mdns_hostname": "compat"}, f)
    with open(os.path.join(root, "data", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"wizard_done": True}, f)

    old_crt = os.path.join(root, "data", "ssl.crt")
    old_key = os.path.join(root, "data", "ssl.key")
    make_old_cert(old_crt, old_key)
    old = x509.load_pem_x509_certificate(open(old_crt, "rb").read())
    check("前置：已放入旧版自签名证书", old.issuer == old.subject, str(old.issuer))
    check("前置：data 目录里还没有 ca.crt / ca.key",
          not os.path.isfile(os.path.join(root, "data", "ca.crt")))

    logf = open(os.path.join(WORK, "a.log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", os.path.join("2.0.0", "app.py")], cwd=root,
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=0x08000000)
    try:
        check("升级后能正常启动", wait_ok("http://127.0.0.1:%d/api/status" % PORT))

        # 根证书是在 HTTPS 服务启动阶段生成的，比 HTTP 接口晚一两秒，必须等
        ca_path = os.path.join(root, "data", "ca.crt")
        for _ in range(60):
            if os.path.isfile(ca_path):
                break
            time.sleep(0.5)
        check("自动生成了根证书 ca.crt", os.path.isfile(ca_path), ca_path)
        ca = x509.load_pem_x509_certificate(open(ca_path, "rb").read())
        try:
            is_ca = ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except Exception:
            is_ca = False
        check("根证书是 CA（iPhone 才能开启完全信任）", is_ca is True)

        for _ in range(60):      # 重签完成的标志：issuer 变成根证书
            try:
                if x509.load_pem_x509_certificate(open(old_crt, "rb").read()).issuer != old.issuer:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        new = x509.load_pem_x509_certificate(open(old_crt, "rb").read())
        check("旧证书已被根证书重签", new.issuer == ca.subject,
              "%s -> %s" % (old.issuer.rfc4514_string()[:24], new.issuer.rfc4514_string()[:24]))

        ip = lan_ip()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=ca_path)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ok_ip = ok_name = False
        detail = ""
        for host, flag in ((ip, "ip"), ("compat.local", "name")):
            try:
                with socket.create_connection(("127.0.0.1", SPORT), timeout=10) as sock:
                    with ctx.wrap_socket(sock, server_hostname=host):
                        pass
                if flag == "ip":
                    ok_ip = True
                else:
                    ok_name = True
            except Exception as exc:
                detail += "%s=%r " % (host, exc)
        check("只信任新根证书时，用 IP 连 HTTPS 通过", ok_ip, detail)
        check("只信任新根证书时，用名字连 HTTPS 通过", ok_name, detail)

        # 旧配置键都在，没有因为升级被清掉
        cfg = json.load(open(os.path.join(root, "data", "config.json"), encoding="utf-8"))
        check("旧配置文件键仍保留", cfg.get("secure_port") == SPORT and cfg.get("mdns_hostname") == "compat",
              cfg)
        # 新入口在
        for path in ("/manual", "/plain", "/cert", "/logs"):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=10) as r:
                    code = r.status
            except Exception as exc:
                code = repr(exc)
            check("新入口 %s 可用" % path, code == 200, code)
    finally:
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        logf.close()

    ok = sum(1 for r in results if r[0])
    print("\n===== 结果：%d/%d 通过 =====" % (ok, len(results)))
    sys.exit(0 if ok == len(results) else 1)


main()
