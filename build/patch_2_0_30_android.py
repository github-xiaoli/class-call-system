# -*- coding: utf-8 -*-
"""patch 2.0.30 - 6：二维码/接入地址改成「IP 优先，主机名备用」。

原因：安卓 / 鸿蒙手机的浏览器（Chrome、华为浏览器等）底层用系统 getaddrinfo
解析域名，而安卓系统解析器**不支持 mDNS**（应用层只能用 NsdManager 发现服务），
所以 http://xxx.local 在安卓上必然解析失败；iOS / macOS / Windows 才支持。
于是二维码默认改成 http://IP:端口，主机名只作为文字备选显示。
"""
import io

# ---------- 1. password.js：pairHostOf 改成 IP 优先 + 额外提供主机名地址 ----------
P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\static\password.js"
s = io.open(P, encoding="utf-8").read()

old = '''function pairHostOf(st) {
  st = st || {};
  // /api/status 返回的是 mdns_hostname；没有就退回局域网 IP，绝不能用 location.hostname
  //（在电脑上打开时那是 127.0.0.1，手机扫了打不开）
  const isIp = (v) => /^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(v || "");
  let host = st.mdns_hostname || "";
  if (!host) {
    const ips = st.local_ips || [];
    const lan = ips.find((ip) => !ip.startsWith("127.") && !ip.startsWith("169.254."));
    host = lan || ips[0] || "";
  }
  if (!host) return "";
  return isIp(host) ? host : host + ".local";
}'''
assert old in s, "pairHostOf not found"

new = '''function lanIpOf(st) {
  st = st || {};
  const ips = st.local_ips || [];
  return ips.find((ip) => !ip.startsWith("127.") && !ip.startsWith("169.254.")) || "";
}

/* 二维码里放的地址：**优先用 IP**。
   安卓 / 鸿蒙手机的浏览器走系统解析器，安卓系统不支持 mDNS，
   http://名字.local 一定解析失败（IP 则一定能打开），所以默认用 IP。
   想用名字的设备（iPhone / Mac / Windows）在页面文字里照样给出来。 */
function pairHostOf(st) {
  st = st || {};
  const ip = lanIpOf(st);
  if (ip) return ip;
  const host = st.mdns_hostname || "";
  if (!host) return "";
  return /^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(host) ? host : host + ".local";
}

/* 备用：用设备名访问的地址（苹果/电脑可用）。 */
function pairNameUrlOf(st) {
  st = st || {};
  const host = st.mdns_hostname || "";
  if (!host || /^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(host)) return "";
  const port = st.port || location.port || 8000;
  return "http://" + host + ".local:" + port + "/pair";
}'''
s = s.replace(old, new, 1)

# 文字提示：同时列出 IP 与设备名两种地址
old2 = '''function pairPageUrlOf(st) {
  st = st || {};
  const host = pairHostOf(st);
  if (!host) return "（未配置设备名）";
  const port = st.port || location.port || 8000;
  return "http://" + host + ":" + port + "/pair";
}'''
new2 = '''function pairPageUrlOf(st) {
  st = st || {};
  const host = pairHostOf(st);
  if (!host) return "（没有可用的局域网地址）";
  const port = st.port || location.port || 8000;
  let text = "http://" + host + ":" + port + "/pair";
  const nameUrl = pairNameUrlOf(st);
  if (nameUrl) text += "　（iPhone/电脑也可用 " + nameUrl + "）";
  return text;
}'''
assert old2 in s
s = s.replace(old2, new2, 1)

# 生成二维码后的提示：说明安卓必须用 IP
old3 = '''    toast("二维码已生成，30 分钟内有效；旧二维码已作废");'''
new3 = '''    toast("二维码已生成（用 IP 直连，安卓/鸿蒙手机也能打开），30 分钟内有效");'''
assert old3 in s
s = s.replace(old3, new3, 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("ok password.js")

# ---------- 2. wizard.js：把局域网 IP 带到前端，phoneUrl 首选 IP ----------
W = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\static\wizard.js"
w = io.open(W, encoding="utf-8").read()

oldw = '''function phoneUrl() {
  // 手机激活/监控需要 https（浏览器只在安全上下文里提供 crypto.subtle）
  let host = state.host || window.__defHost || "127.0.0.1";
  if (host.indexOf(".") < 0) host += ".local";
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ":" + port;
}'''
assert oldw in w, "phoneUrl not found"
neww = '''/* 手机访问的主机名：优先局域网 IP。
   安卓 / 鸿蒙的浏览器不解析 .local（系统解析器无 mDNS），用名字必然打不开；
   用 IP 则所有手机都能打开。设备名仍会显示在页面上供苹果/电脑使用。 */
function phoneHost() {
  const ips = window.__lanIps || [];
  const ip = ips.find((v) => !v.startsWith("127.") && !v.startsWith("169.254."));
  if (ip) return ip;
  let host = state.host || window.__defHost || "127.0.0.1";
  if (host.indexOf(".") < 0) host += ".local";
  return host;
}

function phoneUrl() {
  // 手机激活/监控需要 https（浏览器只在安全上下文里提供 crypto.subtle）
  const host = phoneHost();
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ":" + port;
}

/* 用设备名访问的地址（苹果 / 电脑可用） */
function phoneNameUrl() {
  const host = state.host || window.__defHost || "";
  if (!host || host.indexOf(".") >= 0) return "";
  const useHttps = !!window.__secure;
  const port = useHttps ? (window.__securePort || 9443) : (window.__port || 8000);
  return (useHttps ? "https://" : "http://") + host + ".local:" + port;
}'''
w = w.replace(oldw, neww, 1)

oldw2 = '''  $("qr-url").textContent = state.pairToken
    ? "扫码即自动激活（30 分钟内有效）"
    : url;'''
neww2 = '''  const nameUrl = phoneNameUrl();
  $("qr-url").textContent = (state.pairToken
    ? "扫码即自动激活（30 分钟内有效）"
    : url) + (nameUrl ? "　｜　iPhone/电脑也可用 " + nameUrl : "");'''
assert oldw2 in w
w = w.replace(oldw2, neww2, 1)

oldw3 = '''    window.__defHost = st.hostname || "";'''
neww3 = '''    window.__defHost = st.hostname || "";
    window.__lanIps = st.local_ips || [];'''
assert oldw3 in w
w = w.replace(oldw3, neww3, 1)
io.open(W, "w", encoding="utf-8", newline="\n").write(w)
print("ok wizard.js")

# ---------- 3. /api/wizard/state 带上局域网 IP ----------
A = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
a = io.open(A, encoding="utf-8").read()
olda = '''        "computer_name": os.environ.get("COMPUTERNAME", "") or "这台电脑",'''
newa = '''        "computer_name": os.environ.get("COMPUTERNAME", "") or "这台电脑",
        "local_ips": get_lan_ips(),      # 手机接入用 IP（安卓不解析 .local）'''
assert olda in a
a = a.replace(olda, newa, 1)
io.open(A, "w", encoding="utf-8", newline="\n").write(a)
print("ok app.py wizard state")
