# -*- coding: utf-8 -*-
"""patch 2.0.30 - 9：向导白名单放行 /cert；/plain 加装证书与日志入口；主页白屏兜底。"""
import io

A = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
s = io.open(A, encoding="utf-8").read()

# 1) 向导白名单：/cert（前缀匹配，同时覆盖 /cert.crt 与 /cert.mobileconfig）
old = '''                 # 手机白屏排查用的极简页（不含脚本，最不容易被拦）
                 "/plain", "/ping")'''
assert old in s
new = '''                 # 手机白屏排查用的极简页（不含脚本，最不容易被拦）
                 "/plain", "/ping", "/cert")'''
s = s.replace(old, new, 1)

# 2) /plain 页面里加上「装证书」「看日志」入口
old2 = '''        "<p>如果<b>这一页能看到</b>，但主页白屏，说明是浏览器/证书/缓存的问题：<br>"
        "① 改用 <code>https://主机名.local:%d</code> 再试；"
        "② 清一下浏览器缓存或换个浏览器；③ 在 iPhone 上给浏览器开「本地网络」权限。</p>"'''
assert old2 in s, "plain hint not found"
new2 = '''        "<p>如果<b>这一页能看到</b>，但主页白屏，说明是浏览器/证书/缓存的问题：<br>"
        "① <b>iPhone / iPad 请先到 <a href=/cert>/cert</a> 装一次证书</b>并开启「完全信任」；"
        "② 再试 <code>https://%s:%d</code>（主机名或 IP 都行）；"
        "③ 清一下浏览器缓存；④ 设置 → 隐私与安全性 → 本地网络，允许浏览器访问局域网。</p>"
        "<p>手机访问的每个地址、是否走名字解析，都能在 <a href=/api/health>/api/health</a> "
        "里看到（纯 JSON）；电脑端可开 <a href=/logs>/logs</a> 看运行日志。</p>"'''
s = s.replace(old2, new2, 1)

# 3) /plain 的 % 参数要多一个（主机名 + 端口）
old3 = '''        % (_safe_host(request.scheme), _safe_host(request.host),
           _safe_host(request.remote_addr), _app_version(), items, sp)'''
assert old3 in s, "plain args not found"
new3 = '''        % (_safe_host(request.scheme), _safe_host(request.host),
           _safe_host(request.remote_addr), _app_version(), items,
           _safe_host(normalize_hostname(cfg.get("mdns_hostname", "")) or "主机名"), sp)'''
s = s.replace(old3, new3, 1)

io.open(A, "w", encoding="utf-8", newline="\n").write(s)
print("ok app.py (wizard exempt + plain links)")

# 4) 主页白屏兜底
I = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\templates\index.html"
t = io.open(I, encoding="utf-8").read()
old4 = "<body>"
assert old4 in t
FALLBACK = '''<body>
<!-- 白屏兜底：脚本没跑起来（iPhone 上常见）时，4 秒后自动显示这层，给出可用的下一步 -->
<div id="boot-fallback" style="display:none;position:fixed;inset:0;z-index:99999;background:#fff;
     padding:22px 20px;font-family:'Microsoft YaHei',system-ui,sans-serif;line-height:1.9;color:#0f172a;
     overflow:auto">
  <h2 style="font-size:18px;margin:0 0 8px">页面好像没有加载完（一片空白？）</h2>
  <p style="color:#475569;margin:0 0 10px">先点下面这条，它不依赖脚本，能验证"电脑那边是否正常"：</p>
  <p style="font-size:16px"><a href="/plain">➡️ 打开极简诊断页 /plain</a></p>
  <p style="color:#475569;margin:14px 0 6px"><b>iPhone / iPad 白屏</b>（最常见原因：自签名证书）：
    到 <a href="/cert">/cert</a> 下载证书/描述文件 → 设置 → 通用 → 关于本机 → 证书信任设置 →
    为「班级叫号器本机根证书」开启完全信任，然后重新打开。</p>
  <p style="color:#475569;margin:6px 0">其它可试：清浏览器缓存；设置 → 隐私与安全性 → 本地网络 → 允许浏览器；
    改用 <b>https://本机IP:安全端口</b>。</p>
  <p style="margin-top:14px"><a href="/logs">查看运行日志</a>　·　<a href="/api/health">/api/health</a></p>
</div>
<noscript>
  <div style="padding:22px;font-family:'Microsoft YaHei',sans-serif;line-height:1.9">
    <h2 style="font-size:18px">浏览器禁用了 JavaScript</h2>
    <p>班级叫号器需要 JavaScript。请点这里打开极简页：<a href="/plain">/plain</a>，
       或换用 Safari / Chrome 再打开。</p>
  </div>
</noscript>
<script>
  setTimeout(function () {
    if (window.__appReady) return;
    var d = document.getElementById('boot-fallback');
    if (d) d.style.display = 'block';
  }, 4000);
</script>'''
t = t.replace(old4, FALLBACK, 1)
io.open(I, "w", encoding="utf-8", newline="\n").write(t)
print("ok index.html fallback")

# 5) app.js 标记已就绪
J = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\static\app.js"
j = io.open(J, encoding="utf-8").read()
old5 = '''  fillSettingsForm();
  updateSelectedDisplay();'''
assert old5 in j
new5 = '''  fillSettingsForm();
  updateSelectedDisplay();
  window.__appReady = true;        // 告诉白屏兜底：脚本正常运行，不用显示提示'''
j = j.replace(old5, new5, 1)
io.open(J, "w", encoding="utf-8", newline="\n").write(j)
print("ok app.js __appReady")
