# -*- coding: utf-8 -*-
"""patch 2.0.30 - 8：iOS 白屏根治 —— 自建「本机根证书」+ 描述文件下载 + 白屏兜底页。

思路：
  · 以前签的是**自签名叶子证书**，iOS 上必须每次点「继续访问」；而 WKWebView
    内核的第三方浏览器（Chrome/Edge/夸克…）**没有那个继续访问的按钮**，
    证书一不匹配就直接失败 —— 表现就是"白屏、无任何错误"。
  · 现在改成：一份长期稳定的「本机根证书」+ 由它签发的服务器证书（含 DNS 名字和
    局域网 IP）。手机装一次根证书并开启「完全信任」，之后所有 iOS 浏览器都不白屏。
"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

start = src.index("def _cert_covers(cert_path, names):")
end = src.index("# ---------------------------------------------------------------------------\n# 页面")
assert start < end

NEW = '''CA_CERT_PATH = os.path.join(DATA_DIR, "ca.crt")   # 本机根证书（手机装这个）
CA_KEY_PATH = os.path.join(DATA_DIR, "ca.key")


def _cert_desired_ips():
    """证书里应该包含的 IP：本机所有局域网 IP + 127.0.0.1。

    二维码/接入地址用的是 IP，所以证书里必须有 IP，否则 iPhone 打开
    https://IP:9443 会报"证书名称不匹配"（这个错误不像"不受信任"那样能点继续）。
    """
    ips = set()
    try:
        for ip in get_lan_ips():
            ips.add(ip)
    except Exception:
        pass
    ips.add("127.0.0.1")
    return sorted(ips)


def _load_cert(cert_path):
    from cryptography import x509
    with open(cert_path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _ensure_ca():
    """确保存在一份稳定的「本机根证书」。返回 (cert, key)。"""
    import datetime as _dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    try:
        if os.path.isfile(CA_CERT_PATH) and os.path.isfile(CA_KEY_PATH):
            with open(CA_KEY_PATH, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            return _load_cert(CA_CERT_PATH), key
    except Exception as exc:
        print("[HTTPS] 现有根证书读取失败，重新生成:", exc)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "班级叫号器"),
        x509.NameAttribute(NameOID.COMMON_NAME,
                           "班级叫号器本机根证书 %s" % (socket.gethostname() or "").upper()),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CA_KEY_PATH, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CA_CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print("[HTTPS] 已生成根证书（手机装一次即可长期信任）：%s" % CA_CERT_PATH)
    return cert, key


def _cert_covers(cert_path, names, ips=None):
    """现有证书是否覆盖全部名字 + 当前局域网 IP，且由本机根证书签发。"""
    try:
        from cryptography import x509
        cert = _load_cert(cert_path)
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            return False
        have = {str(n).lower() for n in ext.value.get_values_for_type(x509.DNSName)}
        if not all(n.lower() in have for n in names):
            return False
        if ips:
            import ipaddress as _ip
            have_ip = set()
            for v in ext.value.get_values_for_type(x509.IPAddress):
                try:
                    have_ip.add(str(_ip.ip_address(v)))
                except Exception:
                    pass
            if not all(ip in have_ip for ip in ips):
                return False
        if os.path.isfile(CA_CERT_PATH):
            ca = _load_cert(CA_CERT_PATH)
            if cert.issuer != ca.subject:      # 换过根证书 → 手机上的信任会失效，必须重签
                return False
        return True
    except Exception:
        return False


def _cert_needs_renew(cert_path):
    """iOS 兼容性体检：有效期过长 / 缺 serverAuth / 缺 SAN 都算不合格。"""
    try:
        from cryptography import x509
        cert = _load_cert(cert_path)
        try:
            span = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
        except Exception:
            span = (cert.not_valid_after - cert.not_valid_before).days
        if span > 825:
            print("[HTTPS] 现有证书有效期 %d 天，超过 iOS 允许的 825 天，重新签发" % span)
            return True
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if x509.oid.ExtendedKeyUsageOID.SERVER_AUTH not in eku:
                print("[HTTPS] 现有证书缺少 serverAuth 用途，重新签发")
                return True
        except x509.ExtensionNotFound:
            print("[HTTPS] 现有证书缺少 ExtendedKeyUsage，重新签发")
            return True
        try:
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            print("[HTTPS] 现有证书缺少 SAN，重新签发")
            return True
        return False
    except Exception as exc:
        print("[HTTPS] 证书检查失败，按需重签:", exc)
        return True


def _write_self_signed_cert(cert_path, key_path, names):
    """用本机根证书签发服务器证书（DNS 名字 + 局域网 IP 都写进 SAN），并写入证书链。"""
    import datetime as _dt
    import ipaddress as _ip
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    ca_cert, ca_key = _ensure_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(n) for n in names]
    for ip in _cert_desired_ips():
        try:
            sans.append(x509.IPAddress(_ip.ip_address(ip)))
        except Exception:
            pass
    cn = names[0] if names else socket.gethostname().lower()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    # 有效期 ≤825 天：iOS/iPadOS 与新版 macOS 会直接拒绝过长的服务器证书
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))   # 带上链


def refresh_cert_if_needed():
    """主机名/局域网 IP 变了就重签证书。返回 True 表示证书已更新（HTTPS 要重启才会用上）。"""
    cert_path = os.path.join(DATA_DIR, "ssl.crt")
    key_path = os.path.join(DATA_DIR, "ssl.key")
    names = _cert_desired_names()
    ips = _cert_desired_ips()
    if (os.path.isfile(cert_path) and os.path.isfile(key_path)
            and _cert_covers(cert_path, names, ips)):
        return False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_self_signed_cert(cert_path, key_path, names)
        print("[HTTPS] 已重签证书：%s（IP: %s），重启后 https 生效"
              % (", ".join(names), ", ".join(ips)))
        return True
    except Exception as exc:
        print("[HTTPS] 重签证书失败:", exc)
        return False


def ensure_ssl_context():
    """生成/加载证书（本机根证书 + 服务器证书链），供 HTTPS 安全端口使用。"""
    cert_path = os.path.join(DATA_DIR, "ssl.crt")
    key_path = os.path.join(DATA_DIR, "ssl.key")
    names = _cert_desired_names()
    ips = _cert_desired_ips()
    try:
        _ensure_ca()
    except Exception as exc:
        print("[HTTPS] 生成根证书失败:", exc)
    # iOS/iPadOS 会拒绝有效期过长的证书（>825 天）；旧版本签的 10 年证书在
    # iPhone 上表现就是"Safari 打不开 / 服务器已停止响应"，这里体检并重签。
    if os.path.isfile(cert_path) and _cert_needs_renew(cert_path):
        for _f in (cert_path, key_path):
            try:
                os.remove(_f)
            except Exception:
                pass
    if not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
        _write_self_signed_cert(cert_path, key_path, names)
    elif not _cert_covers(cert_path, names, ips):
        # 名字 / IP 变了，或还是旧的自签名证书 → 用根证书重签
        try:
            _write_self_signed_cert(cert_path, key_path, names)
            print("[HTTPS] 证书已更新（含当前 IP），https 重启后生效")
        except Exception as exc:
            print("[HTTPS] 补签证书失败（继续用旧证书）:", exc)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@app.route("/cert")
def page_cert():
    """手机装证书引导页：iOS 白屏的根治办法（装根证书 + 开启完全信任）。"""
    cfg = get_config()
    return render_template(
        "cert.html", version=_app_version(),
        hostname=normalize_hostname(cfg.get("mdns_hostname", "")),
        lan_ips=get_lan_ips(),
        secure_port=int(_running_secure_port or cfg.get("secure_port", 9443)),
        port=int(_running_port or cfg.get("port", 8000)),
        has_ca=os.path.isfile(CA_CERT_PATH))


@app.route("/cert.crt")
def cert_crt():
    """下载本机根证书（.crt）。iOS/Android 都能装。"""
    try:
        if not os.path.isfile(CA_CERT_PATH):
            _ensure_ca()
        with open(CA_CERT_PATH, "rb") as f:
            data = f.read()
    except Exception as exc:
        return Response("根证书还没生成：%s" % exc, status=500,
                        mimetype="text/plain; charset=utf-8")
    resp = Response(data, mimetype="application/x-x509-ca-cert")
    resp.headers["Content-Disposition"] = 'attachment; filename="classcaller-ca.crt"'
    return resp


@app.route("/cert.mobileconfig")
def cert_mobileconfig():
    """下载 iOS/macOS 描述文件（.mobileconfig）：装上后在证书信任设置里开启完全信任。"""
    import plistlib
    import uuid as _uuid
    try:
        if not os.path.isfile(CA_CERT_PATH):
            _ensure_ca()
        with open(CA_CERT_PATH, "r", encoding="utf-8") as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
        payload = {
            "PayloadType": "Configuration",
            "PayloadVersion": 1,
            "PayloadIdentifier": "com.xiaoli.classcaller.rootca",
            "PayloadUUID": str(_uuid.uuid4()).upper(),
            "PayloadDisplayName": "班级叫号器 本机根证书",
            "PayloadOrganization": "班级叫号器",
            "PayloadDescription": "安装后在「设置 → 通用 → 关于本机 → 证书信任设置」里"
                                  "为「班级叫号器本机根证书」开启完全信任，手机访问就不会白屏。",
            "PayloadRemovalDisallowed": False,
            "PayloadContent": [{
                "PayloadType": "com.apple.security.root",
                "PayloadVersion": 1,
                "PayloadIdentifier": "com.xiaoli.classcaller.rootca.cert",
                "PayloadUUID": str(_uuid.uuid4()).upper(),
                "PayloadDisplayName": "班级叫号器 根证书",
                "PayloadCertificateFileName": "classcaller-ca.cer",
                "PayloadContent": der,
            }],
        }
        data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    except Exception as exc:
        return Response("生成描述文件失败：%s" % exc, status=500,
                        mimetype="text/plain; charset=utf-8")
    resp = Response(data, mimetype="application/x-apple-aspen-config")
    resp.headers["Content-Disposition"] = \
        'attachment; filename="classcaller-ca.mobileconfig"'
    return resp


'''

src = src[:start] + NEW + src[end:]
io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok cert section rewritten")
