# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""自适应码率（ABR）控制器 —— 面向 Cloudflare 隧道与弱网。

关键前提（踩过的坑）：
    经 Cloudflare 隧道时，服务端只连到本机的 cloudflared，ws.send() 永远
    立刻返回（积压进 cloudflared 的缓冲区），所以“发送耗时”完全不能反映
    真实链路状况。唯一可靠的反馈是客户端回执（ack）：

        帧 RTT   = 收到 ack 的时刻 − 该帧发出的时刻
        minRTT   = 最近 10 秒内的最小帧 RTT（链路基线）
        排队延迟 = 帧 RTT − minRTT            ← 真正的拥塞信号
        goodput  = 窗口内已确认字节 / 窗口时长  （≈ 真实可用吞吐）

两个独立的旋钮，各管一件事（混在一起调必然震荡）：
    帧率 ← 由「窗口 / RTT」算出：fps ≈ window / (RTT + 处理时间)
           这样管道刚好填满又不会排队，隧道 RTT 150ms 时自然落到 15fps 左右。
    码率 ← 由 goodput 与排队延迟决定：只动分辨率 / JPEG 质量（AIMD + 滞后防抖）。
"""
import collections
import socket
import threading
import time

# 窗口：从 1（停等）起步，确认不会堆积再逐帧加。上限 8。
# 教训：在 1Mbps 上行的隧道里一次塞 3~4 帧，回执会被自己的帧堵在后面，
# 测出来的 RTT 从 110ms 涨到 800ms，控制器于是误判"拥塞"疯狂降档，
# 结果比老版本还慢。所以窗口只能"涨一点、看回执有没有变慢"这样试探着来。
DEFAULT_WINDOW = 1
WINDOW_MIN = 1
WINDOW_MAX = 8

# 排队延迟判定：超过 minRTT*2.2 + 60ms 才认为真的拥塞。
# 放宽是因为帧 RTT 里含本机的编码时间（远控一帧 30~50ms），波动本来就大，
# 卡太紧会把"自己 CPU 慢"误判成"网络堵"。
QUEUE_RTT_FACTOR = 2.2
QUEUE_RTT_SLACK = 0.060
# 吞吐判定：实测吞吐低于目标码率的 80% 视为喂不饱
GOODPUT_RATIO = 0.8
# 每帧固定开销（编码 + 解码 + 渲染）的经验值，用于把 RTT 换成可跑帧率
FRAME_OVERHEAD = 0.030
# 降/升的时间滞后（秒）
CUT_HOLD = 0.5
RAISE_HOLD = 1.0
# 保险丝：一直在发却这么久没有任何 ack → 压到停等探活
FUSE_IDLE = 1.0
# 在途帧超过这个时间还没回执就"当它丢了"，窗口放行（老版本 ws.receive(timeout=0.4)
# 就是这么干的）。没有这条，链路 ack 一旦慢到 500ms 以上，窗口会把自己锁死，
# 帧率掉到比老版本还低。
STALE_INFLIGHT = 0.4
# 发送速率预算（kbps）。这是最关键的一道闸：隧道/校园网的上行只有 1~20Mbps，
# 一旦推超了，cloudflared 的缓冲区会把回执一起堵在后面，延迟直接几百毫秒起。
# 从保守值起步，凭"回执健康"慢慢加，一有拥塞立刻按 0.6 倍退。
RATE_START_KBPS = 250.0
RATE_MIN_KBPS = 120.0
RATE_MAX_KBPS = 12000.0
RATE_UP = 2.2           # 每轮放大倍数（起步要快，超了会被拥塞检测砍回来）
RATE_DOWN = 0.6         # 拥塞时缩小倍数

# ---------------------------------------------------------------------------
# 延迟预算：用延迟换画质的总开关
#   0.5 秒 = 低延迟优先：一有排队就降档，画面延迟压在 0.3~0.6 秒
#   3~8 秒 = 画质优先：宁可让画面晚几秒，也绝不降分辨率 / 降 JPEG 质量。
#            码率吃满上行，队列允许堆到预算上限，只有被击穿才降档。
# 依据：上行带宽是死的。同样 1.2Mbps，允许 5 秒缓冲能送 720p/q75 的 16KB 帧，
#      只允许 0.5 秒缓冲就只能送 560px/q57 的 5KB 帧——这就是"画质掉了"的根因。
# 默认：监控/图传 5 秒（看板书要清楚），远控 0.5 秒（触控要跟手）。
# ---------------------------------------------------------------------------
DEFAULT_BUDGET_MONITOR = 5.0
DEFAULT_BUDGET_REMOTE = 0.5
BUDGET_MAX = 8.0
QUALITY_FIRST_THRESHOLD = 1.5      # 预算 >=1.5s 即"画质优先"
WINDOW_MAX_HARD = 64               # 画质优先允许的在途帧上限（深队列）
QUALITY_FPS_FLOOR = 0.85           # 帧率掉到"历史最好值"的这个比例以下就降一档画质
QUALITY_FPS_GOOD = 0.97            # 帧率追平历史最好值就再升一档画质
QUALITY_MIN_SCALE = 0.70           # 画质优先的最底档：不低于 70% 宽
QUALITY_MIN_QUALITY = 10           # 画质优先最多只许比基准低 10
# RTT 基线上限：连接刚建立时链路可能已在排队，min_rtt 会被污染成 2~3 秒，
# 拿它当基线会让"超预算"判定永远不成立（控制器变瞎子）。基线最多按 1.5 秒算。
BASELINE_RTT_CAP = 1.5
# 保险丝最长等待（秒）：延迟预算再大，也不能等到 8 秒才判断"对方是不是掉了"
FUSE_IDLE_MAX = 6.0

_STATS_LOCK = threading.Lock()
_LIVE = {}


def enabled(cfg):
    """配置里是否开启自适应码率（默认开）。"""
    try:
        return bool((cfg or {}).get("abr_enabled", True))
    except Exception:
        return True


def budget_of(cfg, which="monitor"):
    """允许的画面延迟预算（秒）。

    which="monitor" 读 latency_budget_monitor（默认 5s，画质优先）
    which="remote"  读 latency_budget_remote （默认 0.5s，低延迟）
    两个都支持用旧的 latency_budget 兜底。
    """
    cfg = cfg or {}
    key = "latency_budget_remote" if which == "remote" else "latency_budget_monitor"
    default = DEFAULT_BUDGET_REMOTE if which == "remote" else DEFAULT_BUDGET_MONITOR
    for k in (key, "latency_budget"):
        if k in cfg:
            try:
                return max(0.2, min(BUDGET_MAX, float(cfg.get(k))))
            except Exception:
                continue
    return default


def is_quality_first(budget):
    return float(budget or 0) >= QUALITY_FIRST_THRESHOLD


def tune_socket(ws):
    """关掉 Nagle：50ms 音频块 / 小控制帧不再等凑包。

    本机→cloudflared 这一段是回环 TCP，Nagle 会额外引入最多 40ms；
    实时流场景一律关掉。
    """
    for obj in (getattr(ws, "sock", None), ws):
        if obj is None:
            continue
        try:
            obj.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return True
        except Exception:
            continue
    return False


def levels_for(width, quality, fps, kind="cam", quality_first=False):
    """按画质预设生成阶梯（低 → 高）。

    quality_first=False（低延迟优先）：4 档，最低到 50% 宽、质量 -24，
        因为要随时能"砍一大截"把延迟压回去。
    quality_first=True（画质优先）：3 档，最低只到 70% 宽、质量 -10，
        保证"画质绝对不会大掉"。
    """
    try:
        width = int(width or 640)
        quality = int(quality or 60)
        fps = int(fps or 15)
    except Exception:
        width, quality, fps = 640, 60, 15
    if quality_first:
        steps = (QUALITY_MIN_SCALE, 0.85, 1.0)
        out = []
        for i, s in enumerate(steps):
            w = max(320, int(round(width * s / 2.0) * 2))
            qmax = 95 if kind != "remote" else 92
            qmin = 58 if kind != "remote" else 42
            q = max(qmin, min(qmax, quality - (len(steps) - 1 - i) * 5))
            out.append({"width": w, "quality": q, "fps": max(12, min(30, fps)),
                        "kind": kind})
        return out
    steps = (0.5, 0.68, 0.85, 1.0)
    out = []
    for i, s in enumerate(steps):
        w = max(320, int(round(width * s / 2.0) * 2))
        if kind == "remote":
            q = max(30, min(90, 34 + i * 4))
        else:
            # 监控：优先保清晰度，低档也不至于糊成马赛克
            q = max(38, min(95, quality - (3 - i) * 8))
        # 帧率不在这里卡死：实际帧率由「窗口 / RTT」现算（见 Controller.level）
        out.append({"width": w, "quality": q, "fps": min(15, max(5, fps)), "kind": kind})
    return out


def levels_for_mode(width, quality, fps, kind="cam", budget=DEFAULT_BUDGET_MONITOR):
    """按延迟预算生成阶梯；画质优先时同时抬高基准。

    预设是"流畅"(640x480/q65) 时，画质优先也至少给到 1280x720 / 质量 72，
    否则"画质再高一点"根本无从谈起（采集端就把天花板锁死了）。
    """
    qf = is_quality_first(budget)
    if qf:
        if kind == "remote":
            width = max(int(width or 0), 1280)
            quality = max(int(quality or 0), 50)
            fps = max(int(fps or 0), 15)
        else:
            width = max(int(width or 0), 1280)
            quality = max(int(quality or 0), 72)
            fps = max(int(fps or 0), 15)
    return levels_for(width, quality, fps, kind=kind, quality_first=qf)


class Controller(object):
    """单条流（一路摄像头或一路屏幕）的自适应控制器。"""

    def __init__(self, name, levels=None, window=DEFAULT_WINDOW, start=None,
                 budget=DEFAULT_BUDGET_MONITOR):
        self.name = str(name or "stream")
        self.levels = list(levels or levels_for(640, 60, 15))
        if not self.levels:
            self.levels = levels_for(640, 60, 15)
        if start is None:
            start = max(0, len(self.levels) - 2)   # 慢启动起点：倒数第二档
        self.idx = max(0, min(len(self.levels) - 1, int(start)))
        try:
            w = int(window)
        except Exception:
            w = DEFAULT_WINDOW
        self.window = WINDOW_MIN                 # 起步就停等，绝不比 2.0.31 差
        self.window_cap = max(WINDOW_MIN, min(WINDOW_MAX, w if w > 1 else WINDOW_MAX))
        self.fps_ref = None                      # 画质优先：历史实际最好帧率（当基准）
        self._lost_mark = 0                      # 上一次看到的 lost 计数（算增量）
        try:
            self.budget = max(0.2, min(BUDGET_MAX, float(budget or DEFAULT_BUDGET_MONITOR)))
        except Exception:
            self.budget = DEFAULT_BUDGET_MONITOR
        self.quality_first = self.budget >= QUALITY_FIRST_THRESHOLD

        self._sent = {}        # seq -> (ts, nbytes)
        self._acked = []       # (ts, nbytes)
        self._sends = []       # 发送时间戳（算 send_fps）
        self._start = time.time()
        self.min_rtt = None
        self._min_reset = self._start
        self.last_rtt = None
        self.last_ack = 0.0
        self.last_send = 0.0
        self.frames = 0
        self.lost = 0
        self.cuts = 0
        self.raises = 0
        self.fuses = 0
        self.reason = "init"
        self._last_cut = 0.0
        self._last_raise = 0.0
        self._last_fuse = 0.0
        self._probed = False
        self.avg_kb = 0.0
        self.rate_kbps = RATE_START_KBPS      # 发送速率预算
        self._over_streak = 0                  # 连续拥塞次数
        self._pause_until = 0.0                # 排空暂停到什么时候

    # ---- 反馈采集 -------------------------------------------------------
    def note_send(self, seq, nbytes):
        now = time.time()
        self._sent[int(seq)] = (now, int(nbytes))
        self.last_send = now
        self.frames += 1
        self._sends.append(now)
        if len(self._sends) > 60:
            del self._sends[:30]

    def send_fps(self, win=2.0):
        """最近 win 秒实际发出的帧率（排障用：看是网络慢还是采集慢）。"""
        now = time.time()
        recent = [t for t in self._sends if now - t <= win]
        if len(recent) < 3:
            return None
        span = recent[-1] - recent[0]
        if span <= 0.2:
            return None
        return round(len(recent) / span, 1)

    def note_ack(self, seq, ts=None):
        """记录一条回执，返回该帧 RTT（秒）；未知序号返回 None。

        ts 是这条 ack **到达**的时间（由 AckReader 打的时间戳）。绝不能
        用"我处理到它的时间"——发送线程可能正在编码/抓图几十毫秒，
        那样算出来的 RTT 会把自己人的耗时当成网络延迟，ABR 就会误判抖动。
        """
        try:
            key = int(seq)
        except Exception:
            return None
        item = self._sent.pop(key, None)
        if item is None:
            return None
        now = ts or time.time()
        rtt = max(0.0, now - item[0])
        self.last_ack = now
        self.last_rtt = rtt
        if self.min_rtt is None or rtt < self.min_rtt:
            self.min_rtt = rtt
        self._acked.append((now, item[1]))
        if len(self._acked) > 240:
            del self._acked[:120]
        kb = item[1] / 1024.0
        self.avg_kb = kb if self.avg_kb <= 0 else (self.avg_kb * 0.8 + kb * 0.2)
        return rtt

    def pending(self):
        """在途帧数（超过 STALE_INFLIGHT 的算丢失，不再占窗口）。"""
        self._prune(time.time())
        return len(self._sent)

    def window_used(self):
        """真正占用窗口的在途帧数：太久没回执的不算（否则慢链路会把自己锁死）。"""
        now = time.time()
        limit = max(STALE_INFLIGHT, 2.0 * (self.min_rtt or 0), self.budget * 0.9)
        return len([1 for _s, (t, _n) in self._sent.items() if now - t <= limit])

    def _prune(self, now):
        if not self._sent:
            return
        # 画质优先时队列本来就很深，在途帧要留够时间，否则回执永远"太晚"被拒
        limit = max(5.0, self.budget * 2.0)
        stale = [s for s, (t, _n) in self._sent.items() if now - t > limit]
        for s in stale:
            self._sent.pop(s, None)
            self.lost += 1

    def goodput(self, win=None):
        """最近 win 秒内已确认的吞吐（kbps）。

        win 必须覆盖至少几个 RTT，否则在隧道下会低估吞吐、误判成拥塞
        （这是第一版疯狂降档的根因）。
        """
        if win is None:
            rtt = self.min_rtt or self.last_rtt or 0.0
            win = max(0.8, min(3.0, 4.0 * rtt))
        now = time.time()
        recent = [(t, n) for t, n in self._acked if now - t <= win]
        if len(recent) < 6:
            return None
        span = recent[-1][0] - recent[0][0]
        if span <= 0.05:
            return None
        return (sum(n for _t, n in recent) * 8 / 1000.0) / span

    def ack_fps(self, win=1.2):
        """最近 win 秒内实际确认的帧率（真实送到客户端的帧率）。"""
        now = time.time()
        recent = [t for t, _n in self._acked if now - t <= win]
        if len(recent) < 4:
            return None
        span = recent[-1] - recent[0]
        if span <= 0.2:
            return None
        return len(recent) / span

    # ---- 决策 -----------------------------------------------------------
    def level(self):
        """当前档位：分辨率/质量来自阶梯，帧率由「窗口 / RTT」现算。

        帧率是"管道刚好填满"的速度：window 帧在途、每帧一个来回。
        window=1 时就是停等（与 2.0.31 等价），window 涨上去才会更快。
        """
        lv = dict(self.levels[self.idx])
        base = max(int(l.get("fps") or 15) for l in self.levels)
        if self.quality_first:
            # 画质优先：帧率不按 RTT 压（延迟由缓冲承担），实际帧率 = 带宽 / 帧大小
            lv["fps"] = base
        else:
            rtt = self.min_rtt or self.last_rtt
            if rtt:
                cap = self.window / max(0.01, rtt)
                lv["fps"] = max(3, min(base, int(cap)))
            else:
                lv["fps"] = base
        lv["window"] = self.window
        lv["base_fps"] = base
        return lv

    def target_kbps(self):
        lv = self.level()
        return lv["fps"] * max(self.avg_kb, 1.0) * 8

    def pace_interval(self, frame_kb, base_fps):
        """本帧之后至少隔多久再发下一帧（秒）：同时受帧率上限与速率预算约束。"""
        by_fps = 1.0 / max(1, int(base_fps or 15))
        by_rate = (max(float(frame_kb or 0.2), 0.2) * 8.0) / max(1.0, self.rate_kbps)
        return max(by_fps, by_rate)

    def pause_left(self, now=None):
        """还需要暂停多久（秒）：用于把 cloudflared 里积压的帧排空。"""
        now = now or time.time()
        return max(0.0, self._pause_until - now)

    def starved(self, now=None):
        """保险丝：一直在发、却连续一段时间没有任何 ack。

        两个教训：
        1) 必须同时看 last_send——暂停推流（页面切后台）时没有发送也没有
           ack，那不是“收不到”，不能触发降档。
        2) 阈值要随 RTT 放大——隧道下正常一帧来回就可能 500ms，
           固定 1 秒会把“网络慢”误判成“对方掉了”。
        """
        now = now or time.time()
        idle = min(FUSE_IDLE_MAX, max(2.0, 5.0 * (self.min_rtt or 0), self.budget * 1.5))
        if not self.last_send or now - self.last_send > idle:
            return False
        base = self.last_ack or self._start
        return (now - base) > idle

    def fuse(self, now=None):
        """压到停等模式探活（不重置档位与基线，避免来回震荡）。"""
        now = now or time.time()
        if now - self._last_fuse < 3.0:
            return False
        self._last_fuse = now
        self.fuses += 1
        self.window = WINDOW_MIN
        self.reason = "fuse(no-ack)"
        self._acked = []
        return True

    def tick(self, now=None):
        """推进一次 AIMD，返回当前档位。

        顺序很重要：先收窗口，再降码率。
        窗口过大是自己造成的排队；只有窗口降到 1 还在排队，才是带宽真不够。
        """
        now = now or time.time()
        self._prune(now)
        if now - self._min_reset > 10.0:      # 基线随时间慢慢刷新
            self._min_reset = now
            if self.last_rtt:
                self.min_rtt = self.last_rtt
        # 长时间没发（页面暂停、摄像头没出帧）之后，基线重新取：
        # 否则被上一次拥塞拉高的 min_rtt 会一直拖着后面所有决策。
        if self.last_send and now - self.last_send > 1.5:
            self.min_rtt = None
            self._probed = False
        # 第一次量到 RTT 就能判断大致是什么样的链路
        if self.min_rtt and not self._probed:
            self._probed = True
            if self.quality_first:
                # 画质优先：直接上顶档，靠缓冲吃满上行
                self.idx = len(self.levels) - 1
                self.rate_kbps = max(self.rate_kbps, 1500.0)
                self.reason = "probe:quality"
            elif self.min_rtt < 0.08:
                # 局域网：别抠，直接用顶档 + 大预算（手机就在旁边）
                self.idx = len(self.levels) - 1
                self.rate_kbps = max(self.rate_kbps, 6000.0)
                self.reason = "probe:lan"
            elif self.min_rtt > 0.20 and self.idx > 1:
                # 高延迟链路（隧道/4G）先保守一档
                self.idx = 1
                self.reason = "probe:high-rtt"

        if self.quality_first:
            return self._tick_quality(now)
        queued = bool(self.last_rtt and self.min_rtt and
                      self.last_rtt > self.min_rtt * QUEUE_RTT_FACTOR + QUEUE_RTT_SLACK)
        # 连续两次才动手，避免单次抖动直接砍档
        if queued:
            self._over_streak += 1
        else:
            self._over_streak = 0
        queued = queued and self._over_streak >= 2
        if queued:
            if now - self._last_cut >= CUT_HOLD:
                if self.window > WINDOW_MIN:
                    self.window -= 1
                    self.reason = "cut:window"
                elif self.idx > 0:
                    self.idx -= 1
                    self.cuts += 1
                    self.reason = "cut:rtt"
                    self._acked = []      # 限速后旧样本不再可信
                # 预算下限不能低于"一帧最多花 2 秒"，否则大帧配小预算会互相拖死
                floor = max(RATE_MIN_KBPS, self.avg_kb * 8.0 / 2.0)
                self.rate_kbps = max(floor, self.rate_kbps * RATE_DOWN)
                # 回执已经落后 1.5 秒以上：cloudflared 里积了一堆帧，光降档救不回来，
                # 必须停发一会儿把队列排空，顺便重取 RTT 基线（否则延迟会一路涨死）
                if self.last_rtt and self.last_rtt > 1.5:
                    self._pause_until = now + 1.5
                    self.min_rtt = None
                    self._probed = False
                    self.window = WINDOW_MIN
                    self._acked = []
                    self.reason = "drain"
                self._last_cut = now
        else:
            want = self.level()["fps"]
            got = self.ack_fps()
            gp = self.goodput()
            bw_short = (got is not None and want >= 6 and got < want * 0.7
                        and len(self._sent) >= self.window)
            # 实测吞吐已经贴着预算（超过 85%）说明预算基本用满，别再加速
            if bw_short:
                if now - self._last_cut >= CUT_HOLD and self.idx > 0:
                    self.idx -= 1
                    self.cuts += 1
                    self.reason = "cut:bw"
                    self._acked = []
                    self._last_cut = now
                    self.rate_kbps = max(RATE_MIN_KBPS, self.rate_kbps * RATE_DOWN)
            elif now - self._last_raise >= RAISE_HOLD and now - self._last_cut >= RAISE_HOLD:
                full = gp is not None and gp > self.rate_kbps * 0.85
                if not full:
                    self.rate_kbps = min(RATE_MAX_KBPS, self.rate_kbps * RATE_UP)
                cap = min(WINDOW_MAX, self.window_cap)
                if self.window < cap and self._acked:
                    self.window += 1          # 先试更大的窗口（帧率随之提高）
                    self.raises += 1
                    self.reason = "raise:window"
                elif self.idx < len(self.levels) - 1 and not full:
                    self.idx += 1             # 窗口到顶了再提画质
                    self.raises += 1
                    self.reason = "raise"
                self._last_raise = now
        return self.level()

    # ---- 画质优先模式 ---------------------------------------------------
    def _tick_quality(self, now):
        """预算内把画质顶到极限：帧率只当路标，用队列深度换分辨率与质量。

        低延迟模式：排队一抬头就降档（保住 0.3~0.6 秒）。
        画质优先：排队可以涨到预算上限，用这段缓冲把码率吃满、画质顶到最高；
                 只有帧率掉太多（上行真吃不消）或预算被击穿才降档。
        """
        gp = self.goodput()
        if gp:
            # 容量估计：把预算提到实测吞吐的 1.15 倍（吃满上行，允许轻微排队）
            self.rate_kbps = max(self.rate_kbps, min(RATE_MAX_KBPS, gp * 1.15))
        # 深队列：预算 × 容量 / 帧大小 = 可以在途多少帧
        try:
            want_win = int(self.rate_kbps * self.budget / 8.0 / max(self.avg_kb, 0.5))
        except Exception:
            want_win = 4
        self.window = max(2, min(WINDOW_MAX_HARD, want_win))
        if self.window > self.window_cap:
            self.window_cap = self.window

        # 基准用"历史实际最好帧率"而不是预设 15fps：
        # 很多摄像头本身只能出 9~10fps，拿 15 当基准会永远判成"帧率不够"而不敢升画质。
        got = self.ack_fps()
        if got is not None and (self.fps_ref is None or got > self.fps_ref):
            self.fps_ref = got
        base = self.fps_ref or max(int(l.get("fps") or 15) for l in self.levels)
        # 基线保护：连接刚建立时链路可能已经在排队（比如上一个会话把上行塞满了），
        # min_rtt 会被污染成 2~3 秒，于是 "rtt > min_rtt + budget" 永远不成立，
        # 控制器就成了瞎子——一直往满队列里塞最高画质。所以基线最多按 1.5 秒算。
        base_rtt = self.min_rtt
        if base_rtt and base_rtt > BASELINE_RTT_CAP:
            base_rtt = BASELINE_RTT_CAP
        over = bool(self.last_rtt and base_rtt and
                    self.last_rtt > base_rtt + self.budget)
        # 丢包信号：有帧被当"过期未回执"清掉了，说明上行在丢包/堵死，必须降档
        d_lost = self.lost - self._lost_mark
        self._lost_mark = self.lost
        if over:
            # 预算被击穿：降一档 + 停发排空，重新收敛
            if now - self._last_cut >= CUT_HOLD:
                if self.idx > 0:
                    self.idx -= 1
                    self.cuts += 1
                    self.reason = "q:cut-budget"
                if gp:
                    self.rate_kbps = max(RATE_MIN_KBPS, min(self.rate_kbps, gp * 0.9))
                self._acked = []
                self._pause_until = now + min(1.5, self.budget * 0.3)
                self._last_cut = now
        elif got is not None and got < base * QUALITY_FPS_FLOOR:
            # 帧率掉太多 = 这一档上行真吃不消，降一档（阶梯最低档就是画质底线）
            if now - self._last_cut >= CUT_HOLD and self.idx > 0:
                self.idx -= 1
                self.cuts += 1
                self.reason = "q:cut-fps"
                self._last_cut = now
        elif got is not None and got >= base * QUALITY_FPS_GOOD:
            if now - self._last_raise >= RAISE_HOLD and now - self._last_cut >= RAISE_HOLD:
                if self.idx < len(self.levels) - 1:
                    self.idx += 1
                    self.raises += 1
                    self.reason = "q:raise"
                    self._last_raise = now
        elif d_lost >= 5 and now - self._last_cut >= CUT_HOLD:
            # 刚有一批帧拿不到回执 → 上行在丢包或堵死：降一档 + 收速率预算
            if self.idx > 0:
                self.idx -= 1
                self.cuts += 1
                self.reason = "q:cut-loss"
            if gp:
                self.rate_kbps = max(RATE_MIN_KBPS, min(self.rate_kbps, gp * 0.8))
            self._acked = []
            self._last_cut = now
        return self.level()

    # ---- 观测 -----------------------------------------------------------
    def stats(self):
        gp = self.goodput()
        lv = self.level()
        return {
            "name": self.name,
            "level": self.idx,
            "levels": len(self.levels),
            "width": lv["width"],
            "quality": lv["quality"],
            "fps": lv["fps"],
            "base_fps": lv.get("base_fps"),
            "window": self.window,
            "pending": len(self._sent),
            "rtt_ms": None if self.last_rtt is None else round(self.last_rtt * 1000, 1),
            "min_rtt_ms": None if self.min_rtt is None else round(self.min_rtt * 1000, 1),
            "goodput_kbps": None if gp is None else round(gp, 1),
            "ack_fps": None if self.ack_fps() is None else round(self.ack_fps(), 1),
            "send_fps": self.send_fps(),
            "target_kbps": round(self.target_kbps(), 1),
            "rate_kbps": round(self.rate_kbps, 1),
            "avg_frame_kb": round(self.avg_kb, 2),
            "frames": self.frames,
            "lost": self.lost,
            "cuts": self.cuts,
            "raises": self.raises,
            "fuses": self.fuses,
            "reason": self.reason,
            "budget_s": self.budget,
            "quality_first": self.quality_first,
            "age_s": round(time.time() - self._start, 1),
        }

    def log(self, tag=""):
        s = self.stats()
        print("[ABR] %s%s lvl=%d/%d %dpx q=%d fps=%d win=%d pending=%d rtt=%sms min=%sms "
              "send=%sfps ack=%sfps gp=%skbps target=%skbps frame=%skB frames=%d lost=%d "
              "cut=%d up=%d fuse=%d bud=%ss %s"
              % (tag or "", s["name"], s["level"], s["levels"] - 1, s["width"], s["quality"],
                 s["fps"], s["window"], s["pending"], s["rtt_ms"], s["min_rtt_ms"],
                 s["send_fps"], s["ack_fps"], s["goodput_kbps"], s["target_kbps"],
                 s["avg_frame_kb"], s["frames"],
                 s["lost"], s["cuts"], s["raises"], s["fuses"],
                 s["budget_s"], s["reason"]))
        return s


# ---------------------------------------------------------------------------
# 独立读线程：给每条收到的消息打上“到达时间戳”
# ---------------------------------------------------------------------------
class AckReader(threading.Thread):
    """把回执与控制消息从发送线程里分出来。

    发送线程每帧要抓图/编码（几十毫秒），如果在那里顺便读回执，测出来的
    RTT 会包含自己的耗时，隧道下尤其明显（看起来像拥塞，其实是本地慢）。
    """

    def __init__(self, ws, ctrl=None, auto=False):
        super().__init__(daemon=True, name="ws-ack-reader")
        self.ws = ws
        self.ctrl = ctrl
        self.auto = bool(auto)            # 是否在这里顺便推进自适应（发送线程可能被 send 挡住）
        self.msgs = collections.deque()   # 未处理的控制消息 (ts, text)
        self.lock = threading.Lock()
        self.stop = False
        self.closed = False

    def run(self):
        while not self.stop:
            try:
                msg = self.ws.receive(timeout=0.05)
            except Exception:
                break
            if msg is None:
                if getattr(self.ws, "connected", True) is False:
                    break
                continue
            ts = time.time()
            if isinstance(msg, str):
                m = msg.strip()
                if m[:1] == "a" and self.ctrl is not None:
                    self.ctrl.note_ack(m[1:], ts=ts)
                    if self.auto:
                        # 发送线程可能正卡在 ws.send（上行只有 1Mbps 时一帧要几百 ms），
                        # 所以降档判断必须在这里也跑一遍，否则收敛得非常慢。
                        self.ctrl.tick(ts)
                        if self.ctrl.starved(ts):
                            self.ctrl.fuse(ts)
                elif m:
                    with self.lock:
                        self.msgs.append((ts, m))
            # 二进制输入帧（老客户端把触控当二进制发）忽略
        self.closed = True

    def drain(self):
        with self.lock:
            out = list(self.msgs)
            self.msgs.clear()
        return out


# ---------------------------------------------------------------------------
# 全局注册表：/api/abr/stats 直接读，方便线上排查与自动化测试
# ---------------------------------------------------------------------------
def register(ctrl):
    with _STATS_LOCK:
        _LIVE[ctrl.name] = ctrl
    return ctrl


def unregister(name):
    with _STATS_LOCK:
        _LIVE.pop(name, None)


def snapshot():
    with _STATS_LOCK:
        items = list(_LIVE.values())
    return [c.stats() for c in items]
