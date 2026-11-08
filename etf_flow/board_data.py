# -*- coding: utf-8 -*-
"""ETF 资金观测看板的数据层（不依赖 Flask，便于单测与本地回放）。

页面 /etf_flow 需要两类数据：

``board``
    原版看板 data.js 里的 9 个 island（initData / cmpData / dailyData / typData /
    histData / postHol / broadData / fflowData / cumData），由东财策略看板定期重建。
    这里原样透传，前端渲染脚本取数与独立版完全一致，换版时无需改前端。

``bundle``
    盘中行情：20 只 ETF + 上证综指的当日分钟序列与实时快照。打包成一次请求，
    因为页面每轮刷新会要 21 个标的的分钟线 —— 逐个转发就是 21 次客户端请求，
    既撞 25/min 的爬虫限额，也让浏览器排队。

东财 IPv6 边缘节点对脚本请求 TLS 后空响应，模块级强制 IPv4（与 etf_flow_api 一致）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests
import urllib3.util.connection as _urllib3_conn

_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

# 可选的「历史净流入看板」上游。默认关闭：仓库自带静态种子，开箱即可离线出图。
# 若你有自己的同构看板，设 ETF_BOARD_ORIGIN=https://your-host 即可接回历史 island
# 与第四路行情兜底；不设则所有 board_* 路径直接短路，不发起任何外部请求。
BOARD_ORIGIN = os.getenv("ETF_BOARD_ORIGIN", "").strip().rstrip("/")
BOARD_URL = (BOARD_ORIGIN + "/data.js") if BOARD_ORIGIN else ""
BOARD_KLINE = (BOARD_ORIGIN + "/api/kline?symbol={sym}&scale=1&datalen=260") if BOARD_ORIGIN else ""
BOARD_HQ = (BOARD_ORIGIN + "/api/hq?codes={codes}") if BOARD_ORIGIN else ""

EM_TRENDS = (
    "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    "?secid={secid}&ndays=1&iscr=0&ut=" + "fa5fd1943c7b386f172d6893dbfba10b"
    + "&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
)
EM_HQ = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&secids={secids}&fields=f2,f3,f6,f12,f14,f18"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}

# 备用行情源：新浪。东财是按「整台机器出口 IP」掐的（一掐就是全部标的 RemoteDisconnected），
# 探针一旦失败就整轮不走东财，改由新浪兜住 21 个标的的分钟线与快照。
SINA_KLINE = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    "?symbol={sym}&scale=1&datalen=300"
)
SINA_HQ = "https://hq.sinajs.cn/list={syms}"
_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn",
}

# 第三路：腾讯。东财是按出口 IP 在网络层掐（换子域也是 000），新浪快照对机房 IP 403，
# 腾讯这两条接口在机房 IP 下都是 200 —— 它是唯一能在被封机器上正常出数的一路。
# 分钟线给的是「累计成交额」，要自己做差分得到每分钟成交额（与东财 f57 同口径）。
TENCENT_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
TENCENT_HQ = "https://qt.gtimg.cn/q={syms}"
_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://gu.qq.com/",
}

# 默认不读系统代理（本机代理常把行情请求劫持成空响应）；需要走代理时设 ETF_TRUST_ENV=1
_session = requests.Session()
_session.trust_env = os.getenv("ETF_TRUST_ENV", "0") == "1"

# island 的顶层 key 独占一行，缩进不固定：rebuild_from_xlsx 生成的是两格，
# 后续手工补进去的（如 fflowData）常常零缩进。只按「行首 → 名字 → 冒号」匹配。
_ISLAND_KEY_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*):", re.M)

# 页面渲染必需的 island；缺一个就没法出图，宁可判为不可用去走兜底
REQUIRED_ISLANDS = ("initData", "cmpData", "dailyData", "broadData", "cumData")


def sym_of(secid: str) -> str:
    """1.000001 → sh000001 ；0.159915 → sz159915 。"""
    parts = str(secid).split(".")
    if len(parts) == 2:
        return ("sh" if parts[0] == "1" else "sz") + parts[1]
    return str(secid)


def fetch_board_islands(timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """抓原版 data.js 并拆成 island 字典；不可达或结构变化时返回 None。"""
    if not BOARD_URL:
        return None
    try:
        resp = _session.get(BOARD_URL, headers=_HEADERS, timeout=timeout)
        text = resp.text
    except Exception:
        return None

    if not _ISLAND_KEY_RE.search(text):
        return None

    islands: Dict[str, Any] = {}
    try:
        matches = list(_ISLAND_KEY_RE.finditer(text))
        tail = text.rindex("};")
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else tail
            islands[m.group(1)] = json.loads(text[start:end].strip().rstrip(","))
    except (ValueError, IndexError):
        return None
    if any(k not in islands for k in REQUIRED_ISLANDS):
        return None
    return islands


# 分类堆积图的 12 类配色。外部看板的 broadData 自带 colors，
# 静态种子里没有；渲染脚本会直接读 BR.colors[i % BR.colors.length]，
# 缺了这个字段整段渲染会抛异常，连下面的分类表格一起空掉。
_BROAD_COLORS = [
    "#2b5fd9", "#d93630", "#e8862e", "#0aa06e", "#8a6fd1", "#c9a227",
    "#3c8dbc", "#c2557a", "#6b8e23", "#7b5ea7", "#d07b2a", "#2f9e8f",
]


def _broad_from_seed(broad: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把种子的 broad 补成渲染脚本认得的完整结构：colors + totalCum。"""
    out = dict(broad or {})
    if not out.get("dates"):
        return out
    if not out.get("colors"):
        out["colors"] = list(_BROAD_COLORS)
    if not out.get("totalCum"):
        running, cum = 0.0, []
        for value in out.get("total") or []:
            try:
                running += float(value)
            except (TypeError, ValueError):
                pass
            cum.append(round(running, 2))
        out["totalCum"] = cum
    return out


def islands_from_seed(seed: Dict[str, Any]) -> Dict[str, Any]:
    """原版不可达时，用静态种子拼出同构 island —— 页面照常出图，只是少分钟底稿。"""
    seed = seed or {}
    daily = seed.get("daily") or {}
    dates: List[str] = list(daily.get("dates") or [])
    amount: List[Any] = list(daily.get("amount") or [])
    netflow: List[Any] = list(daily.get("netflow") or [])
    funds = seed.get("funds") or []

    latest: Dict[str, Any] = {}
    for i in range(len(dates) - 1, -1, -1):
        if i < len(netflow) and netflow[i] is not None:
            latest = {
                "date": dates[i],
                "amount": amount[i] if i < len(amount) else None,
                "netflow": netflow[i],
            }
            break

    table = [
        {"code": f.get("code"), "name": f.get("name"), "amount": None, "pct": None}
        for f in funds
    ]
    return {
        # times 为空 → 前端跳过静态底稿，直接等第一轮实时数据
        "initData": {"preClose": None, "date": "", "times": [], "bars": [], "closes": [], "table": table},
        "cmpData": {
            "today": {"date": dates[-1], "amount": amount[-1]} if dates and amount else {},
            "recent": seed.get("recent") or [],
            "latest": latest,
            "support": seed.get("support") or [],
            "excluded": seed.get("excluded"),
        },
        "dailyData": {"dates": dates, "amount": amount, "netflow": netflow, "idx": daily.get("idx") or []},
        "typData": seed.get("typ") or {},
        "broadData": _broad_from_seed(seed.get("broad")),
        "cumData": seed.get("cum") or {},
    }


# ---------------------------------------------------------------- 源健康度与熔断
#
# 页面每分钟要 21 只标的的分时，多实例部署时上游请求会成倍放大。被风控的代价
# 不是「慢一下」，而是整台机器的出口 IP 被封（一旦被封，整轮行情都会返空），
# 之后每一轮都在同一个封口上白等。所以这里记着每个源的状态：整片失败就冷却，
# 冷却期内直接不碰它，把请求分给其它源。
#
# 只作用于进程内：gunicorn 2 个 worker 各学一遍，代价是故障发生时要多疼一轮，
# 换来的是不用为一份缓存状态再引入跨进程锁。

_SOURCE_ORDER = ("eastmoney", "tencent", "sina")
_SOURCE_COOLDOWN_S = 300.0     # 整片失败后 5 分钟内不再尝试该源
_HEALTH: Dict[str, Dict[str, Any]] = {}

# 同一个源的请求之间拉开最小间隔。风控盯的不是日总量，是「短时间内的并发脉冲」：
# 实测 8 并发 × 40 个请求几十秒内就把本机 IP 打进腾讯的 501 反爬挑战页，
# 而同样的总量摊到每分钟里则完全无事。所以宁可慢一两秒，也不要打出脉冲。
_SOURCE_MIN_GAP = {"eastmoney": 0.12, "tencent": 0.20, "sina": 0.10}
_THROTTLE_LOCK = threading.Lock()
_NEXT_SLOT: Dict[str, float] = {}


def _throttle(source: str) -> None:
    """给同一源的下一次请求占一个时间片；取号在锁内完成，睡眠在锁外。"""
    gap = _SOURCE_MIN_GAP.get(source, 0.0)
    if gap <= 0:
        return
    with _THROTTLE_LOCK:
        now = time.time()
        slot = max(now, _NEXT_SLOT.get(source, 0.0))
        _NEXT_SLOT[source] = slot + gap
    wait = slot - now
    if wait > 0:
        time.sleep(wait)


def reset_source_health() -> None:
    """清空熔断状态（单测与手工排障用）。"""
    _HEALTH.clear()


def source_health() -> Dict[str, Dict[str, Any]]:
    """各源当前状态，给运维接口/排障看：cooling 表示还有多少秒才恢复尝试。"""
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
    for name in _SOURCE_ORDER:
        st = _HEALTH.get(name) or {}
        left = max(0.0, float(st.get("cool_until") or 0.0) - now)
        out[name] = {
            "cooling": left > 0,
            "cool_left_s": int(round(left)),
            "fails": int(st.get("fails") or 0),
            "last_ok": st.get("last_ok"),
        }
    return out


def _is_cooling(name: str) -> bool:
    return float((_HEALTH.get(name) or {}).get("cool_until") or 0.0) > time.time()


def _cool_source(name: str, seconds: float = _SOURCE_COOLDOWN_S) -> None:
    st = _HEALTH.setdefault(name, {})
    st["cool_until"] = time.time() + seconds
    st["fails"] = int(st.get("fails") or 0) + 1


def _mark_ok(name: str) -> None:
    st = _HEALTH.setdefault(name, {})
    st["cool_until"] = 0.0
    st["fails"] = 0
    st["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _healthy_sources() -> List[str]:
    return [s for s in _SOURCE_ORDER if not _is_cooling(s)]


def _split_even(items: List[str], sources: List[str]) -> Dict[str, List[str]]:
    """把 items 轮转铺到各源上，尽量均分。"""
    buckets: Dict[str, List[str]] = {s: [] for s in sources}
    if not sources:
        return buckets
    for i, item in enumerate(items):
        buckets[sources[i % len(sources)]].append(item)
    return buckets


def _plan_shards(secids: List[str], sources: List[str]) -> Dict[str, List[str]]:
    """把标的轮转分给各源；指数固定给首选源，图和表格的基准口径保持一致。

    轮转而不是切块，是因为 21 只里一旦某个源被封，轮转能把损失摊到每一段，
    切块则会出现「某一段整段缺失」。
    """
    if not sources or not secids:
        return {s: [] for s in sources}
    shards = _split_even(secids[:-1], sources)
    shards[sources[0]].append(secids[-1])      # build_bundle 保证最后一个是 index_secid
    return shards


def _fetch_one(source: str, secid: str, timeout: float = 6.0, retries: int = 2):
    """按源名分发到具体抓取函数，统一吃 secid、统一返回 {preClose, bars}。"""
    _throttle(source)
    if source == "eastmoney":
        return _fetch_trends(secid, timeout=timeout, retries=retries)
    if source == "tencent":
        return _fetch_trends_tencent(sym_of(secid), timeout=timeout)
    if source == "sina":
        return _fetch_trends_sina(sym_of(secid), timeout=timeout)
    return None


def _fetch_shard(source: str, secids: List[str], timeout: float = 6.0, workers: int = 4):
    """拉一个源分到的整片分钟线 → ({secid: trends}, 整片是否失败)。

    拿第一只当探针：源被封时第一次就断，让剩下 10 只各等满超时纯属浪费。
    探针不通就直接判整片失败，交给调用方换源 + 熔断。
    """
    got: Dict[str, Any] = {}
    if not secids:
        return got, False
    head = _fetch_one(source, secids[0], timeout=min(timeout, 3.0), retries=1)
    if not (head and head.get("bars")):
        # 只有切片够大才认定「源挂了」。补洞时往往只剩 1-2 只，探针失败更可能是
        # 「这个源本来就没有这只标的」，按源故障处理会误伤一个健康的源。
        return got, len(secids) >= 3
    got[secids[0]] = head
    rest = secids[1:]
    if rest:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(rest)))) as pool:
            for secid, tr in zip(rest, pool.map(
                    lambda s: _fetch_one(source, s, timeout=timeout), rest)):
                if tr and tr.get("bars"):
                    got[secid] = tr
    # 一片里过半拿不到：不是个别标的没数据，是这个源开始拦了（腾讯会先只拦一部分），
    # 趁早判整片失败去熔断，别让后面几轮继续往同一个墙上撞
    if len(rest) >= 2 and len(got) * 2 < len(secids):
        return got, True
    return got, False


def _fetch_trends(secid: str, timeout: float = 6.0, retries: int = 2) -> Optional[Dict[str, Any]]:
    """东财当日分时 → {"preClose": float|None, "bars": [{day, close, amount}]}。

    只保留 close / amount：页面画分钟柱与折线只用这两个字段，
    21 个标的 × 240 根若带全字段，JSON 会从 200KB 涨到 500KB。

    东财对高频脚本请求会直接掐连接（RemoteDisconnected），重试一次基本能救回来，
    连续两次失败才认账 —— 21 只里缺一两只，页面会按成功率决定是否重绘。
    探针（探一轮东财到底通不通）用 retries=1：封 IP 时第一次就断，等第二次是白等。
    """
    raw, pre_close = None, None
    for attempt in range(max(1, retries)):
        try:
            resp = _session.get(EM_TRENDS.format(secid=secid), headers=_HEADERS, timeout=timeout)
            block = (resp.json() or {}).get("data") or {}
            raw = block.get("trends") or []
            pre_close = block.get("preClose")
            if raw:
                break
        except Exception:
            raw = None
        if attempt == 0:
            time.sleep(0.25)

    if not raw:
        return None

    out = []
    for line in raw:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        try:
            # 东财时间戳精确到分钟：2026-11-03 09:31
            stamp = parts[0].strip()
            if len(stamp) == 16:
                stamp += ":00"
            out.append({
                "day": stamp,
                "close": round(float(parts[2]), 3),
                "amount": round(float(parts[6]), 0),
            })
        except (TypeError, ValueError, IndexError):
            continue
    if not out:
        return None
    return {"preClose": pre_close, "bars": out}


def _fetch_trends_sina(sym: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """新浪分钟线 → 与东财同构的 {"preClose", "bars"}。

    datalen=300 会跨到前一交易日，所以按日期分组后只留最后一天，
    昨收取前一交易日最后一根的 close（东财是接口直接给 preClose）。
    """
    try:
        resp = _session.get(SINA_KLINE.format(sym=sym), headers=_SINA_HEADERS, timeout=timeout)
        rows = resp.json()
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    by_day: Dict[str, List[Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("day") or "")
        if len(day) < 16:
            continue
        by_day.setdefault(day[:10], []).append(row)
    if not by_day:
        return None

    days = sorted(by_day)
    pre_close = None
    if len(days) >= 2:
        try:
            pre_close = round(float(by_day[days[-2]][-1]["close"]), 3)
        except (KeyError, TypeError, ValueError, IndexError):
            pre_close = None

    out = []
    for row in by_day[days[-1]]:
        try:
            out.append({
                "day": str(row["day"])[:16] + ":00",
                "close": round(float(row["close"]), 3),
                "amount": round(float(row.get("amount") or 0), 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return None
    return {"preClose": pre_close, "bars": out}


def _fetch_trends_tencent(sym: str, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    """腾讯当日分时 → 与东财同构的 {"preClose", "bars"}。

    返回行是 ``0930 4.086 51874 21195716.00``：时间 / 现价 / 累计成交量 / 累计成交额。
    页面画分钟柱要的是「当分钟成交额」，所以对累计值做差分（首根直接取累计）。
    时间戳只有 HHMM，日期由同一响应里的 ``date``（20261104）补全。
    昨收腾讯这里不给，由上层的快照补进 pre_by_sym。
    """
    try:
        resp = _session.get(TENCENT_MINUTE.format(sym=sym), headers=_TENCENT_HEADERS, timeout=timeout)
        block = ((resp.json() or {}).get("data") or {}).get(sym) or {}
    except Exception:
        return None
    inner = block.get("data") or {}
    raw = inner.get("data") or []
    day = str(inner.get("date") or "")
    if not raw or len(day) != 8:
        return None
    stamp_day = "%s-%s-%s" % (day[:4], day[4:6], day[6:])

    out: List[Dict[str, Any]] = []
    prev_cum: Optional[float] = None
    for line in raw:
        parts = str(line).split()
        if len(parts) < 4:
            continue
        try:
            hhmm = parts[0]
            close = float(parts[1])
            cum = float(parts[3])
        except (TypeError, ValueError):
            continue
        if len(hhmm) != 4:
            continue
        amount = cum if prev_cum is None else cum - prev_cum
        prev_cum = cum
        if amount <= 0:
            # 腾讯在午休边界固定塞一根 13:00 的空 bar（成交额 0、价格沿用 11:30），
            # 东财与新浪都没有这一根。留着它，综合图会在 13:00 多出一个只算 7 只的点。
            continue
        out.append({
            "day": "%s %s:%s:00" % (stamp_day, hhmm[:2], hhmm[2:]),
            "close": round(close, 3),
            "amount": round(max(amount, 0.0), 0),
        })
    if not out:
        return None
    return {"preClose": None, "bars": out}


def _build_hq_tencent(secids: List[str], timeout: float = 6.0) -> Dict[str, Any]:
    """腾讯快照 → 与东财 ulist 同构的 diff。一次请求拿全部标的，GBK。

    字段（``~`` 分隔）：1 名称 / 3 现价 / 4 昨收 / 32 涨跌幅% / 37 成交额(万元)。
    涨跌幅自己按昨收算：非交易时段接口给的是 0，直接透传会让表格显示成平盘。
    """
    if not secids:
        return {"rc": 0, "data": {"diff": []}}
    try:
        resp = _session.get(TENCENT_HQ.format(syms=",".join(sym_of(s) for s in secids)),
                            headers=_TENCENT_HEADERS, timeout=timeout)
        text = resp.content.decode("gbk", errors="ignore")
    except Exception:
        return {"rc": 0, "data": {"diff": []}}

    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        match = re.search(r'v_(?:sh|sz)(\d{6})="([^"]*)"', line)
        if not match:
            continue
        f = match.group(2).split("~")
        if len(f) < 38:
            continue

        def num(idx: int):
            try:
                value = f[idx].strip()
                return None if value in ("", "-") else float(value)
            except (TypeError, ValueError, IndexError):
                return None

        price, pre = num(3), num(4)
        amount_wan = num(37)
        out.append({
            "f2": price,
            "f3": round((price / pre - 1) * 100, 2) if price and pre else num(32),
            "f6": amount_wan * 10000 if amount_wan is not None else None,
            "f12": match.group(1),
            "f14": f[1] or match.group(1),
            "f18": pre,
        })
    return {"rc": 0, "data": {"diff": out}}


def _build_hq_sina(secids: List[str], timeout: float = 8.0) -> Dict[str, Any]:
    """新浪快照 → 与东财 ulist 同构的 diff（f2 现价 / f3 涨跌幅 / f6 成交额 / f18 昨收）。"""
    if not secids:
        return {"rc": 0, "data": {"diff": []}}
    try:
        resp = _session.get(SINA_HQ.format(syms=",".join(sym_of(s) for s in secids)),
                            headers=_SINA_HEADERS, timeout=timeout)
        text = resp.content.decode("gbk", errors="ignore")
    except Exception:
        return {"rc": 0, "data": {"diff": []}}

    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        match = re.search(r'hq_str_(?:sh|sz)(\d{6})="([^"]*)"', line)
        if not match:
            continue
        parts = match.group(2).split(",")
        if len(parts) < 10:
            continue

        def num(idx: int):
            try:
                return float(parts[idx])
            except (TypeError, ValueError, IndexError):
                return None

        price, pre = num(3), num(2)
        out.append({
            "f2": price,
            "f3": round((price / pre - 1) * 100, 2) if price and pre else None,
            "f6": num(9),
            "f12": match.group(1),
            "f14": parts[0],
            "f18": pre,
        })
    return {"rc": 0, "data": {"diff": out}}


def _build_hq(secids: List[str], timeout: float = 6.0) -> Dict[str, Any]:
    """实时快照（最新价 / 涨跌幅 / 成交额 / 昨收）。失败返回空 diff，前端保留上一帧。"""
    if not secids:
        return {"rc": 0, "data": {"diff": []}}
    try:
        resp = _session.get(EM_HQ.format(secids=",".join(secids)), headers=_HEADERS, timeout=timeout)
        diff = ((resp.json() or {}).get("data") or {}).get("diff") or []
    except Exception:
        return {"rc": 0, "data": {"diff": []}}

    out = []
    for row in diff:
        if not isinstance(row, dict):
            continue

        def num(v):
            try:
                return None if v in (None, "-", "") else float(v)
            except (TypeError, ValueError):
                return None

        out.append({
            "f2": num(row.get("f2")),
            "f3": num(row.get("f3")),
            "f6": num(row.get("f6")),
            "f12": row.get("f12"),
            "f14": row.get("f14"),
            "f18": num(row.get("f18")),
        })
    return {"rc": 0, "data": {"diff": out}}


def _hq_from_bars(secids: List[str], funds: List[Dict[str, Any]], kline: Dict[str, Any],
                  pre_by_sym: Dict[str, Any]) -> Dict[str, Any]:
    """没有可用的快照接口时，用分钟线自己算：现价=最后一根，涨跌幅对昨收，成交额求和。

    hq.sinajs.cn 对机房 IP 直接 403（按 IP 段防盗链），但分钟线已经拿到了，
    表格要的三个字段全能从它推出来，没必要为此再挂一个源。
    """
    name_by_code = {f.get("code"): f.get("name") for f in (funds or [])}
    diff: List[Dict[str, Any]] = []
    for secid in secids:
        sym = sym_of(secid)
        bars = kline.get(sym) or []
        if not bars:
            continue
        code = sym[2:]
        pre = pre_by_sym.get(sym)
        close = bars[-1].get("close")
        diff.append({
            "f2": close,
            "f3": round((close / pre - 1) * 100, 2) if close and pre else None,
            "f6": round(sum(b.get("amount") or 0 for b in bars), 0),
            "f12": code,
            "f14": name_by_code.get(code) or ("上证指数" if sym == "sh000001" else code),
            "f18": pre,
        })
    return {"rc": 0, "data": {"diff": diff}}


def _fetch_snapshot(secids: List[str], timeout: float = 6.0):
    """实时快照：东财 ulist → 腾讯 qt → 新浪 hq，一次请求覆盖全部标的。

    快照必须最先拿：它带着每只的昨收（f18），后面的缺口补数和合成快照都要这个基准。
    三个都是批量接口，所以这一层永远只花 1 次请求，慢也不会慢到哪去。
    """
    for name, fetcher in (("eastmoney", _build_hq), ("tencent", _build_hq_tencent),
                          ("sina", _build_hq_sina)):
        if _is_cooling(name):
            continue
        try:
            hq = fetcher(secids, timeout=timeout)
        except Exception:
            hq = {"rc": 0, "data": {"diff": []}}
        if (hq.get("data") or {}).get("diff"):
            _mark_ok(name)
            return hq, name
    return {"rc": 0, "data": {"diff": []}}, ""


def _absorb(kline: Dict[str, Any], pre_by_sym: Dict[str, Any], got: Dict[str, Any]) -> None:
    for secid, tr in (got or {}).items():
        if not (tr and tr.get("bars")):
            continue
        sym = sym_of(secid)
        kline[sym] = tr["bars"]
        if tr.get("preClose"):
            pre_by_sym[sym] = tr["preClose"]


def _minute_gap(a: str, b: str) -> int:
    """两个「YYYY-MM-DD HH:MM:SS」相隔多少分钟。跨午休会偏大，只用来判断是否「差一两分钟」。"""
    try:
        return abs((int(a[11:13]) * 60 + int(a[14:16])) - (int(b[11:13]) * 60 + int(b[14:16])))
    except (ValueError, IndexError):
        return 999


def _align_window(kline: Dict[str, Any], min_cover: float = 0.8) -> None:
    """把各标的裁到「至少 min_cover 都有数据」的公共时间窗，原地修改。

    分片取数后各源的时间口径会差一点：东财从 09:30（集合竞价那根）起，新浪从 09:31
    起，末根偶尔差一两分钟。综合图是按分钟把 21 只的成交额相加的 —— 窗口不齐，
    首尾那几根就会少算几千万，柱子上看就是「开头矮半截、末尾突然掉下去」。

    - 首端裁到多数标的的起点：新浪 09:31 那根本身已含集合竞价（实测 181.5M ≈
      东财 09:30 的 21.2M + 09:31 的 157.7M），裁掉东财的 09:30 不会丢量；
    - 末端若只有个别标的慢一两分钟，跟着最慢的走（宁可晚一分钟，也不要最后一根缺几只）；
      慢太多则按多数标的的末根收，否则一张图会被一个卡住的源拖停。
    """
    bars = [b for b in kline.values() if b]
    if len(bars) < 3:
        return
    firsts = sorted(b[0]["day"] for b in bars)
    lasts = sorted(b[-1]["day"] for b in bars)
    keep = max(1, int(len(bars) * min_cover + 0.999))
    start = firsts[min(keep, len(firsts)) - 1]
    end = lasts[len(lasts) - keep]
    if end > lasts[0] and _minute_gap(lasts[0], end) <= 2:
        end = lasts[0]
    if start >= end:
        return
    for sym, rows in list(kline.items()):
        if not rows:
            continue
        trimmed = [b for b in rows if start <= b["day"] <= end]
        if trimmed:
            kline[sym] = trimmed


def build_bundle(funds: List[Dict[str, Any]], index_secid: str, timeout: float = 6.0,
                 workers: int = 4) -> Dict[str, Any]:
    """一次性取齐 21 个标的的分时与快照，键为 sh/sz 代码。

    取数不再「死认一个源再兜底」，而是按源健康度分片并发：

    1. 快照先拿（东财 ulist → 腾讯 qt → 新浪 hq，都是批量接口，1 次请求）；
    2. 21 只按轮转分给当前健康的源（东财 / 腾讯 / 新浪），互不干扰；
    3. 某个源整片失败 → 当场熔断 5 分钟，剩下的标的转给其它源补；
    4. 快照全空 → 用分钟线合成；分钟线全空 → 看板 /api 最后一道。

    这样单源压力从「每台机器每分钟 22 次」降到 1/3，且被封的源不会被反复重试。
    """
    secids = [f["secid"] for f in funds if f.get("secid")] + [index_secid]

    # 1) 快照：一次请求，顺带把每只的昨收带回来
    hq, hq_source = _fetch_snapshot(secids, timeout=timeout)
    code_to_sym = {s.split(".")[-1]: sym_of(s) for s in secids}
    pre_by_sym: Dict[str, Any] = {}
    for row in (hq.get("data") or {}).get("diff") or []:
        sym, pre = code_to_sym.get(str(row.get("f12"))), row.get("f18")
        if sym and pre:
            pre_by_sym[sym] = pre

    # 2) 分钟线：按源轮转分片，各源并发拉自己那一片
    sources = _healthy_sources() or ["tencent"]   # 全被熔断时仍给最稳的一路，别整站停摆
    shards = _plan_shards(secids, sources)
    kline: Dict[str, Any] = {}
    stats: Dict[str, int] = {}
    dead = set()
    # 每只标的「哪个源已经对它失败过」：分片阶段的失败也要记，否则补洞时
    # 会又挑回同一个源，白撞一次
    failed: Dict[str, set] = {s: set() for s in sources}

    if len(sources) == 1:
        batches = [(sources[0],) + _fetch_shard(sources[0], shards[sources[0]],
                                                timeout=timeout, workers=workers)]
    else:
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            futs = {s: pool.submit(_fetch_shard, s, shards[s], timeout, workers) for s in sources}
            batches = [(s,) + futs[s].result() for s in sources]

    for name, got, gone in batches:
        if got:
            _absorb(kline, pre_by_sym, got)
            stats[name] = stats.get(name, 0) + len(got)
        for secid in shards[name]:
            if secid not in got:
                failed[name].add(secid)
        if gone:
            dead.add(name)
            _cool_source(name)
        else:
            _mark_ok(name)

    # 3) 补洞：缺的按剩余源再均衡分一次。不能顺序往第一个源上堆 —— 东财一熔断，
    #    它那 8 只全压给腾讯就成了「腾讯 15 只、新浪 6 只」，等于把压力转嫁给下一个。
    #    每只记下「哪个源已经对它失败过」，下一轮换源，不去重复撞同一堵墙。
    for _ in range(2):
        missing = [s for s in secids if sym_of(s) not in kline]
        alive = [s for s in sources if s not in dead]
        if not missing or not alive:
            break
        buckets: Dict[str, List[str]] = {s: [] for s in alive}
        load = {s: 0 for s in alive}
        for secid in missing:
            cands = [s for s in alive if secid not in failed[s]]
            if not cands:
                continue
            # 挑当前分到最少的那个源，避免把缺口集中到某一路
            pick = min(cands, key=lambda s: (load[s], alive.index(s)))
            buckets[pick].append(secid)
            load[pick] += 1

        progressed = False
        for name, part in buckets.items():
            if not part:
                continue
            got, gone = _fetch_shard(name, part, timeout=timeout, workers=workers)
            if got:
                _absorb(kline, pre_by_sym, got)
                stats[name] = stats.get(name, 0) + len(got)
                progressed = True
            for secid in part:
                if secid not in got:
                    failed[name].add(secid)
            if gone:
                dead.add(name)
                _cool_source(name)
        if not progressed and not any(buckets[s] for s in alive if s not in dead):
            break

    # 4) 各源时间口径对齐，综合图首尾几分钟才不会少算几只
    _align_window(kline)

    pre_close = pre_by_sym.get(sym_of(index_secid))

    # 5) 快照一个都没拿到：现价/涨跌/成交额全都能从分钟线推出来
    if not (hq.get("data") or {}).get("diff") and kline:
        hq = _hq_from_bars(secids, funds, kline, pre_by_sym)

    # 6) 三路分钟线全空 → 看板 /api（现返回 401，只留作最后一道）
    if not kline:
        kline, pre_close = _build_bundle_via_board(secids, timeout=timeout, workers=workers)
        stats = {"board": len(kline)} if kline else {}
        if kline and not (hq.get("data") or {}).get("diff"):
            hq = _hq_via_board(secids, timeout=timeout)

    source = ",".join("%s:%d" % (name, cnt) for name, cnt in stats.items()) or "none"
    return {
        "kline": kline,
        "hq": hq,
        "index_pre_close": pre_close,
        "source": source,
        "hq_source": hq_source,
        "sources_health": source_health(),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "missing": [s for s in secids if sym_of(s) not in kline],
    }


def _bars_from_board_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """看板 /api/kline 的行 → 本模块的 bar 结构，只保留最后一个交易日。"""
    last_day = None
    for row in rows:
        if isinstance(row, dict) and row.get("day"):
            last_day = str(row["day"])[:10]
    out = []
    for row in rows:
        try:
            day = str(row["day"])
        except (KeyError, TypeError):
            continue
        if last_day and day[:10] != last_day:
            continue
        try:
            stamp = day[:16]
            if len(stamp) == 16:
                stamp += ":00"
            out.append({
                "day": stamp,
                "close": round(float(row["close"]), 3),
                "amount": round(float(row["amount"]), 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_trends_board(sym: str, timeout: float = 8.0) -> Optional[List[Dict[str, Any]]]:
    if not BOARD_KLINE:
        return None
    try:
        resp = _session.get(BOARD_KLINE.format(sym=sym), headers=_HEADERS, timeout=timeout)
        rows = resp.json()
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    bars = _bars_from_board_rows(rows)
    return bars or None


def _build_bundle_via_board(secids: List[str], timeout: float = 8.0, workers: int = 6):
    if not BOARD_KLINE:
        return {}, None
    syms = [sym_of(s) for s in secids]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: _fetch_trends_board(s, timeout=timeout), syms))
    kline = {sym: bars for sym, bars in zip(syms, results) if bars}
    return kline, None


def _hq_via_board(secids: List[str], timeout: float = 8.0) -> Dict[str, Any]:
    if not BOARD_HQ:
        return {"rc": 0, "data": {"diff": []}}
    try:
        resp = _session.get(BOARD_HQ.format(codes=",".join(sym_of(s) for s in secids)),
                            headers=_HEADERS, timeout=timeout)
        body = resp.json() or {}
    except Exception:
        return {"rc": 0, "data": {"diff": []}}
    diff = ((body.get("data") or {}).get("diff") or [])
    return {"rc": 0, "data": {"diff": diff}}
