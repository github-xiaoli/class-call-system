# -*- coding: utf-8 -*-
"""把「环境与设备限制」并进图文说明书（build/guide/body.html），并重排章节号、目录。

改动：
  · 新增 十四、连不上的原因（环境与设备限制）→ id=s14
  · 原「十四、开源与致谢」→ 十五、开源与致谢（id=s15）
  · 目录补一条 s14、把原 s14 链接改为 s15
  · 常见问题补 Q8~Q10（指向新章节）
  · 页脚版本号 2.0.19 → 2.0.30
"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\build\guide\body.html"
s = io.open(P, encoding="utf-8").read()

# ---------- 1. 新增章节（插在「开源与致谢」之前） ----------
anchor = '<h2 id="s14">十四、开源与致谢</h2>'
assert anchor in s, "找不到致谢章节锚点"

CHAPTER = '''<h2 id="s14">十四、连不上的原因（环境限制）</h2>
<p>这一章只写<b>软件改不了、只能靠现场设置或换方式</b>的问题。凡是能在程序里解决的，都已经解决了，
不在此列。遇到打不开时，先照 14.1 的表找症状，再看对应小节。</p>

<h3>14.1 症状速查表</h3>
<table>
  <tr><th style="width:33%">你看到的症状</th><th style="width:34%">真正的原因</th><th>只能这样解决</th></tr>
  <tr><td>手机输 <span class="kbd">http://名字.local:8000</span> 提示"找不到服务器"</td>
      <td>这台设备/系统<b>不解析</b> <span class="kbd">.local</span>（见 14.2）</td>
      <td>改用 <span class="kbd">http://电脑IP:8000</span></td></tr>
  <tr><td>iPhone / iPad 打开就<b>白屏、没有任何报错</b></td>
      <td>自签证书 + 第三方浏览器没有"继续访问"按钮（见 14.3）</td>
      <td>用 Safari，或装证书并开启「完全信任」</td></tr>
  <tr><td>Safari 能开、Chrome / Edge 白屏</td><td>同上，不是权限问题</td><td>同上</td></tr>
  <tr><td>昨天能开，今天打不开（IP 没变）</td>
      <td>校园 AP 关了多播 / 开了客户端隔离（见 14.4）</td>
      <td>用 IP；换网络；或手机热点</td></tr>
  <tr><td>二维码突然失效</td><td>电脑 IP 变了（DHCP 租约，见 14.5）</td>
      <td>路由器做 DHCP 保留；或重新生成二维码</td></tr>
  <tr><td>手机连得上电脑，但电脑收不到请求</td>
      <td>Windows 防火墙"公用网络"拦入站（见 14.6）</td><td>首次运行点"允许访问"；或放行端口</td></tr>
  <tr><td>在线更新一直失败</td><td>学校网络屏蔽 GitHub</td><td>用安装包手动更新</td></tr>
</table>

<h3>14.2 手机打不开 <span class="kbd">名字.local</span>：取决于系统，不是浏览器</h3>
<p><span class="kbd">.local</span> 这种名字要靠设备自己用 mDNS 解析。能不能解析，看的是
<b>操作系统/ROM</b> —— 同一个 Edge，在不同设备上结果就不同：</p>
<table>
  <tr><th style="width:34%">设备</th><th style="width:36%">系统</th><th>能否解析 .local</th></tr>
  <tr><td>荣耀 30S</td><td>鸿蒙 3.0（AOSP 10 底子）</td><td>不能</td></tr>
  <tr><td>Lenovo TB310FU</td><td>LineageOS 23 GSI（Android 16）</td><td>能</td></tr>
  <tr><td>iPhone / iPad / Mac</td><td>iOS / iPadOS / macOS</td><td>能</td></tr>
  <tr><td>Windows 10 / 11 电脑</td><td>系统自带 mDNS</td><td>能</td></tr>
</table>
<p>所以"换浏览器能解决"是错觉：Chrome、Edge、夸克在安卓上都走同一个系统解析器。</p>
<p class="tip"><b>结论：手机一律用 IP。</b>软件里的二维码、配对地址、向导里的手机地址
<b>默认已经都用 IP</b>，<span class="kbd">.local</span> 只作为文字备选。
想确认某台设备行不行：让它分别打开 <span class="kbd">http://电脑IP:8000/plain</span> 与
<span class="kbd">http://名字.local:8000/plain</span>，能出同一页就是支持。</p>

<h3>14.3 iPhone / iPad 白屏：装一次证书就彻底解决</h3>
<p><b>原因有两个，叠在一起：</b>① 软件用的是自签名证书，苹果默认不信任；② Safari 遇到证书问题会弹
"显示详细信息 → 访问此网站"，而 iPhone 上的 Chrome / Edge / 夸克<b>没有这个界面</b>，
于是请求直接失败 —— 表现就是<b>白屏、没有任何错误提示</b>。</p>
<p>另外苹果有硬性规定：<b>手动安装的证书不会自动受信任</b>，必须在「证书信任设置」里手动打开「完全信任」。</p>
<p><b>操作（一次就好）：</b></p>
<ol class="steps">
  <li>iPhone 用 <b>Safari</b> 打开 <span class="kbd">https://电脑IP:安全端口/cert</span>
      （例如 <span class="kbd">https://192.168.1.19:9443/cert</span>）</li>
  <li>点「① 下载描述文件（iPhone / iPad 推荐）」→ 弹窗选<b>允许</b></li>
  <li>设置 → 已下载描述文件（或：通用 → VPN 与设备管理）→ <b>安装</b> → 输锁屏密码</li>
  <li><b>设置 → 通用 → 关于本机 → 证书信任设置</b> → 打开
      「班级叫号器本机根证书」的<b>完全信任</b></li>
</ol>
<p class="tip">做完这四步，Safari、Chrome、Edge、夸克<b>都不会再白屏</b>，也不用每次点"继续"。
装完记得<b>完全关掉浏览器再重开</b>；还在白屏就是第 4 步没开。</p>
<p class="note">说明：<span class="kbd">/cert</span>、<span class="kbd">/cert.mobileconfig</span>、
<span class="kbd">/cert.crt</span> 是 <b>2.0.30 新增</b>的入口。2.0.29 及更早版本打开会是 404，
请先更新。安卓 / 鸿蒙装证书的方法在同一页里也有。</p>

<h3>14.4 「本地网络」权限：找不到 Safari 是正常的</h3>
<p>「设置 → 隐私与安全性 → 本地网络」管的是<b>第三方 App</b> 访问局域网（iOS 14 起的隐私机制）。
Safari 是系统浏览器，不走这套权限模型，<b>永远不会出现在列表里</b>。</p>
<p>列表里只会出现：① 你装过的第三方 App；② 它<b>已经尝试过一次</b>局域网访问
（弹过"想查找并连接本地网络上的设备"之后）。所以列表里没有 Chrome / Edge 时：
先用它打开一次 <span class="kbd">http://电脑IP:8000</span>，弹窗选允许，再回来看。</p>
<p>另外三个会干扰局域网访问的手机开关，建议关掉：</p>
<table>
  <tr><th style="width:32%">开关</th><th style="width:40%">路径</th><th>建议</th></tr>
  <tr><td>iCloud <b>私密中继</b></td><td>设置 → 顶部 Apple ID → iCloud → 私密中继</td><td>关闭</td></tr>
  <tr><td><b>限制 IP 地址跟踪</b></td><td>设置 → 无线局域网 → 当前 Wi-Fi 右侧 ⓘ</td><td>关闭</td></tr>
  <tr><td><b>私有无线局域网地址</b></td><td>同上</td><td>关闭（会重连一次 Wi-Fi）</td></tr>
</table>
<p class="note">还有一条硬限制：<b>iOS 拒绝有效期超过 825 天的服务器证书</b>。旧版本签过 10 年证书，
在 iPhone 上就是"打不开 / 服务器已停止响应"。2.0.30 起改为"根证书 10 年 + 服务器证书 365 天"，
从很旧版本升级上来的电脑第一次启动会自动重签，<b>手机端需要重新装一次证书</b>。</p>

<h3>14.5 网络与路由器：多播、隔离、网段、IP 变化</h3>
<table>
  <tr><th style="width:30%">情况</th><th style="width:36%">现象</th><th>处理</th></tr>
  <tr><td>校园 AP 过滤多播（UDP 5353）</td>
      <td>名字时好时坏，重启手机又好一会儿</td>
      <td>这类网络里 <span class="kbd">.local</span> 永远不可靠，直接用 IP</td></tr>
  <tr><td>客户端隔离 / 无线隔离</td><td>手机能上网，但连教室电脑也不行（IP 也不行）</td>
      <td>换不做隔离的网络（办公室网 / 自建路由器 / 手机热点）</td></tr>
  <tr><td>不同网段 / VLAN</td><td>IP 和名字都打不开</td><td>让手机与电脑在同一网段</td></tr>
  <tr><td>电脑 IP 变了（DHCP 租约）</td><td>之前生成的二维码失效</td>
      <td>见下面"固定 IP"的三种做法</td></tr>
</table>
<p><b>固定 IP 的三种做法：</b></p>
<ul>
  <li><b>路由器里做 DHCP 保留（MAC 绑定）——最推荐。</b>电脑仍用"自动获取"，路由器每次发同一个 IP，
      <b>零冲突风险</b>，换网络也不影响。</li>
  <li><b>静态 IP，且必须选在 DHCP 池外</b>（例如池是 100–199，就用 .50 或 .240），
      掩码 / 网关 / DNS 都要填对。Windows 的静态 IP 是绑在<b>网卡</b>上的，不是绑 Wi-Fi 名，
      白天连学校网、晚上连热点的电脑会有一边不通。</li>
  <li><b>什么都不改</b>：每次用面板上显示的当前 IP，或当场重新生成二维码。</li>
</ul>
<p class="tip">⚠️ <b>静态 IP 千万不要填在 DHCP 池里</b>：路由器不知道你手动占了它，会把同一地址发给别人，
两台设备轮流掉线（Windows 会提示"IP 地址冲突"并跳到 169.254）。</p>
<p>另外：学校网络屏蔽 GitHub 时，软件内置的四个镜像全部不通 → 在线更新必然失败，改用安装包手动更新即可；
系统时间不准也会导致证书校验和更新校验失败，把电脑和手机都设为自动对时。</p>

<h3>14.6 电脑端：防火墙与网卡</h3>
<ul>
  <li><b>防火墙"公用网络"拦入站。</b>软件第一次运行会弹"是否允许访问"，必须允许（专用 + 公用都勾）。
      坑在于<b>规则是按当时的网络类型记的</b>：之后连上一个被判为"公用"的网络（学校 Wi-Fi 常被判公用），
      规则就不生效 → 手机又连不上，重新允许一次即可。</li>
  <li><b>多网卡 / 虚拟网卡。</b>电脑常有 Hyper-V 的 <span class="kbd">vEthernet</span>、WSL、VMware、
      VPN、Wi-Fi Direct（<span class="kbd">本地连接* 2</span>），这些 IP 手机永远连不上。
      2.0.30 起用系统原生 API 判断"是否有默认网关 / 是否虚拟"，默认只广播真实网卡，
      设置里的<b>「mDNS 诊断」</b>会列出每块网卡被跳过的原因。</li>
  <li><b>组策略禁用 PowerShell。</b>2.0.29 及更早版本靠 PowerShell 枚举网卡，被禁用时会退化成
      "所有 IP 一起广播"；<b>2.0.30 起不再依赖 PowerShell</b>。</li>
  <li><b>没有管理员权限 / 禁止安装。</b>用绿色版（zip 解压即用），或安装包的测试模式
      （装到当前用户目录，不提权）。</li>
  <li><b>Edge 弹「现有实例正在以提升的权限运行」。</b>与软件无关：软件以管理员启动后拉起的 Edge
      继承了管理员权限，之后普通权限再开 Edge 就会问。点是（Y），或把那个 Edge 全部关掉。</li>
  <li><b>摄像头 / 麦克风被占用。</b>监控、对讲、远控需要它们；被钉钉、腾讯会议、希沃占用或被隐私设置
      禁止时不可用，关掉占用程序再试。</li>
</ul>

<h3>14.7 应急四招（改程序之外）</h3>
<ol class="steps">
  <li><b>用 IP</b>：手机打开 <span class="kbd">http://电脑IP:8000</span>（叫号全功能）；
      <span class="kbd">https://电脑IP:9443</span>（激活 / 监控 / 远控）。</li>
  <li><b>先开 <span class="kbd">/plain</span></b>：不含脚本的极简页，能打开就说明网络与服务正常，
      白屏是浏览器/证书的问题。</li>
  <li><b>再看 <span class="kbd">/api/health</span></b>：返回 JSON，能看出你连到的是哪台电脑、
      走的 http 还是 https、你的 IP 和浏览器。</li>
  <li><b>电脑上看 <span class="kbd">/logs</span></b>：实时运行日志，可一键导出 txt
      （稳定版主页不显示入口，直接输网址可进）。</li>
</ol>
<p class="tip"><b>讲台贴纸版（可直接打印）：</b><br>
手机打不开时按顺序试：① 地址换成 IP <span class="kbd">http://电脑IP:8000</span>；
② 打开 <span class="kbd">http://电脑IP:8000/plain</span>，能出页面说明电脑没问题；
③ iPhone 白屏：Safari 打开 <span class="kbd">https://电脑IP:9443/cert</span> → 下载描述文件 → 安装 →
设置/通用/关于本机/证书信任设置 → 打开"完全信任"；
④ 还是不行：看电脑上的 <span class="kbd">/logs</span>，把内容发给管理员。</p>
<p class="note">完整版（含 25 条明细、为什么改程序解决不了、逐条对策）见仓库
<span class="kbd">docs/环境与设备限制.md</span>。</p>

'''

s = s.replace(anchor, CHAPTER + '<h2 id="s15">十五、开源与致谢</h2>', 1)

# ---------- 2. 目录 ----------
old_toc = '''    <li><a href="#s13">常见问题</a></li>
    <li><a href="#s14">开源与致谢</a></li>'''
assert old_toc in s, "找不到目录锚点"
new_toc = '''    <li><a href="#s13">常见问题</a></li>
    <li><a href="#s14">连不上的原因（环境限制）</a></li>
    <li><a href="#s15">开源与致谢</a></li>'''
s = s.replace(old_toc, new_toc, 1)

# ---------- 3. 常见问题补三条（插在 FAQ 的 </dl> 之前，指向新章节） ----------
i_end = s.index('<h2 id="s15">十五、开源与致谢</h2>')
i_dl = s.rindex("</dl>", 0, i_end)
EXTRA = """
<dt>Q8. iPhone / iPad 打开一片白，什么提示都没有？</dt>
<dd>这是自签证书的问题：iPhone 上的 Chrome / Edge 没有"继续访问"按钮，所以直接白屏。
用 Safari 打开 <span class="kbd">https://电脑IP:9443/cert</span> 装一次证书并开启"完全信任"即可，
详见 <a href="#s14">第十四章 14.3</a>。</dd>

<dt>Q9. 手机输"名字.local"打不开，输 IP 就能开？</dt>
<dd>那是这台设备/系统不解析 <span class="kbd">.local</span>（例如鸿蒙 3.0 的荣耀 30S），
与浏览器无关。手机一律用 IP，详见 <a href="#s14">第十四章 14.2</a>。</dd>

<dt>Q10. 昨天还能用，今天手机就连不上了？</dt>
<dd>最常见两种：电脑 IP 变了（旧二维码失效）、或校园 AP 限制多播 / 开了客户端隔离。
见 <a href="#s14">第十四章 14.5</a>；电脑上的 <span class="kbd">/logs</span> 能看到换网与重新注册的记录。</dd>
"""
s = s[:i_dl] + EXTRA + s[i_dl:]

# ---------- 4. 页脚版本 ----------
s = s.replace("版本 2.0.19", "版本 2.0.30").replace("版本 2.0.30", "版本 2.0.30", 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("body.html 已更新：", len(s), "字符")
