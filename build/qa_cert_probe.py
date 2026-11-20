# -*- coding: utf-8 -*-
"""证书探针（用软件自带的 python 跑，它有 cryptography）：验证根证书/描述文件/证书链。

用法：
    python build\qa_cert_probe.py <http_base> <https_port> <expect_ip> <expect_name> <work_dir>
输出一行 JSON 到 stdout。
"""
import json
import os
import plistlib
import socket
import ssl
import sys
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import ExtendedKeyUsageOID

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8760"
SPORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9760
EXP_IP = sys.argv[3] if len(sys.argv) > 3 else ""
EXP_NAME = sys.argv[4] if len(sys.argv) > 4 else "iostest"
WORK = sys.argv[5] if len(sys.argv) > 5 else "."

out = {"ok": False}


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read(), dict(r.headers)


try:
    os.makedirs(WORK, exist_ok=True)
    ca_pem, ca_hdr = fetch(BASE + "/cert.crt")
    ca_path = os.path.join(WORK, "ca_probe.crt")
    with open(ca_path, "wb") as f:
        f.write(ca_pem)
    ca = x509.load_pem_x509_certificate(ca_pem)
    out["cert_ct"] = ca_hdr.get("Content-Type") or ""
    out["cert_cd"] = ca_hdr.get("Content-Disposition") or ""
    try:
        out["ca_is_ca"] = bool(
            ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except Exception as exc:
        out["ca_is_ca"] = False
        out["ca_err"] = repr(exc)
    out["ca_cn"] = ca.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    out["ca_fp"] = ca.fingerprint(SHA256()).hex()
    out["ca_not_after"] = str(ca.not_valid_after_utc if hasattr(ca, "not_valid_after_utc")
                              else ca.not_valid_after)

    mc_raw, mc_hdr = fetch(BASE + "/cert.mobileconfig")
    out["mc_ct"] = mc_hdr.get("Content-Type") or ""
    mc = plistlib.loads(mc_raw)
    var = (mc.get("PayloadContent") or [{}])[0]
    out["mc_type"] = var.get("PayloadType")
    out["mc_desc"] = mc.get("PayloadDescription") or ""
    der = var.get("PayloadContent")
    if isinstance(der, (bytes, bytearray)):
        out["mc_fp"] = x509.load_der_x509_certificate(bytes(der)).fingerprint(SHA256()).hex()
    else:
        out["mc_fp"] = ""

    # ---- 只用这张根证书当信任锚去连 HTTPS（= iPhone 装好并完全信任后的行为）----
    def tls(hostname):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=ca_path)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        with socket.create_connection(("127.0.0.1", SPORT), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ts:
                return ts.getpeercert(binary_form=True)

    for label, host in (("ip", EXP_IP), ("name", EXP_NAME + ".local")):
        try:
            der_leaf = tls(host)
            out["tls_" + label] = True
            if "leaf" not in out:
                leaf = x509.load_der_x509_certificate(der_leaf)
                try:
                    span = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
                except Exception:
                    span = (leaf.not_valid_after - leaf.not_valid_before).days
                san = leaf.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value
                ips = []
                for v in san.get_values_for_type(x509.IPAddress):
                    try:
                        ips.append(str(v))
                    except Exception:
                        pass
                dns = [str(v) for v in san.get_values_for_type(x509.DNSName)]
                try:
                    eku = leaf.extensions.get_extension_for_class(
                        x509.ExtendedKeyUsage).value
                    has_auth = ExtendedKeyUsageOID.SERVER_AUTH in eku
                except Exception:
                    has_auth = False
                out["leaf"] = {"span": span, "ips": ips, "dns": dns,
                               "server_auth": has_auth,
                               "issuer_eq_ca": leaf.issuer == ca.subject,
                               "cn": leaf.subject.get_attributes_for_oid(
                                   x509.oid.NameOID.COMMON_NAME)[0].value}
        except Exception as exc:
            out["tls_" + label] = False
            out["tls_" + label + "_err"] = repr(exc)

    out["ok"] = True
except Exception as exc:
    out["error"] = repr(exc)

print(json.dumps(out, ensure_ascii=True))
