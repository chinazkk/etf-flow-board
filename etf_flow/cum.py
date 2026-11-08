# -*- coding: utf-8 -*-
"""ETF 资金看板「昨日线 / 近20日均值」自建数据源。

问题背景（实测核验）：
- `cumData` 原本来自外部看板 data.js 的 cumData island；
- 外部源可能被人机验证挡住或停止服务，此时 `fetch_board_islands()` 返回 None；
- 若只降级到 seed 的 `cum` 快照，`prevDate` 会永远停在种子生成日。

本模块不再依赖那个被验证码挡死的上游，而是自行抓取东财 5 分钟 K：
对 seed 中的 20 只宽基 ETF，按其 5 分钟 bar 成交额（区间结束时刻）做
「边界精确值 + 中间线性插值」累计，得到上一完整交易日（含昨天）与近 20
交易日均值的 241 点分钟累计曲线，结构与原 `cumData` 完全一致，可直接覆盖。

插值口径与原上游 `tools/build_cum.py` 一致（前端注释里也写明了这一点），
不能退回前向填充 —— 那会让曲线变成 4 分钟一格的台阶。

数据口径（已实测，勿换源）：
    GET https://push2his.eastmoney.com/api/qt/stock/kline/get
        ?secid={secid}&fields1=f1,f2&fields2=f51,f57&klt=5&fqt=0&end=20500101&lmt=1300
- klines 每行 "2026-11-05 14:35,123456789.000"，第 2 个字段成交额，单位元；
- lmt=1300 实测返回约 26 个交易日，足够覆盖「上一交易日 + 近 20 交易日」。

可靠性：
- 并发抓取（并发 8、单只超时 8s、失败重试 2 次并轮换镜像域名）；
- 成功标的 < 80% 直接放弃整轮（宁可不更新也不写残缺数据）；
- 磁盘缓存到 /tmp（多 worker 共享、重启不丢），带 TTL + 单飞，不阻塞请求。
"""
from __future__ import annotations

import json
import math
import logging
import os
import socket
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

import requests
import urllib3.util.connection as _urllib3_conn

_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

logger = logging.getLogger(__name__)

CUM_PATH = os.getenv("ETF_CUM_CACHE") or os.path.join(
    tempfile.gettempdir(), "etf_flow_cum.json"
)
BUILD_VERSION = 2

SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "etf_flow_seed.json")

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}
_EM_SESSION = requests.Session()
_EM_SESSION.trust_env = False

_UT = "fa5fd1943c7b386f172d6893dbfba1ab"

_MIRROR_HOSTS = [
    "push2his.eastmoney.com",
    "1.push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "32.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
]

# 最小成功标的数（20 只的 80% = 16）
_MIN_OK = 16
# 单只标的抓取超时（秒）
_FETCH_TIMEOUT = 8.0


def _slots() -> List[str]:
    """241 个槽位：09:30..11:30 + 13:01..15:00。"""
    out = []
    for h in range(9, 12):
        for m in range(0, 60):
            t = "%02d:%02d" % (h, m)
            if "09:30" <= t <= "11:30":
                out.append(t)
    for h in range(13, 16):
        for m in range(0, 60):
            t = "%02d:%02d" % (h, m)
            if "13:01" <= t <= "15:00":
                out.append(t)
    return out


SLOTS: List[str] = _slots()
_SLOT_MAP: Dict[str, int] = {t: i for i, t in enumerate(SLOTS)}


def _slot_index(hhmm: str) -> int:
    """HH:MM → 241 槽位下标；无法匹配返回 -1。

    槽位 0 = 09:30，依序到 240 = 15:00。下午从 13:01（下标 121）开始。
    时间戳不在列表里（如 13:00）→ 取第一个 >= 它的槽位（就近归入后续槽位）。
    """
    if len(hhmm) != 5 or ":" not in hhmm:
        return -1
    idx = _SLOT_MAP.get(hhmm, -1)
    if idx >= 0:
        return idx
    i = 0
    while i < len(SLOTS) and SLOTS[i] < hhmm:
        i += 1
    return i if i < len(SLOTS) else -1


def _fetch_kline(secid: str, timeout: float = _FETCH_TIMEOUT) -> Optional[List[Tuple[str, str, float]]]:
    """抓单只标的 5 分钟 K，返回 [(date, HH:MM, 成交额(元))]，失败返回 None。

    最多重试 2 次，每次换一个镜像域名（实测 1./7./32./63. 均可用）。
    """
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        "?secid=%s&fields1=f1,f2&fields2=f51,f57&klt=5&fqt=0&end=20500101&lmt=1300&ut=%s"
        % (secid, _UT)
    )
    for attempt in range(2):
        host = _MIRROR_HOSTS[attempt % len(_MIRROR_HOSTS)]
        u = url.replace("push2his.eastmoney.com", host)
        try:
            r = _EM_SESSION.get(u, headers=_EM_HEADERS, timeout=timeout)
            payload = r.json()
        except Exception:
            continue
        block = (payload or {}).get("data") or {}
        klines = block.get("klines") or []
        if not klines:
            continue
        out = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            stamp = parts[0].strip()
            try:
                amount = float(parts[1])
            except (TypeError, ValueError):
                continue
            # 时间戳形如 "2026-11-05 14:35"，取 date 与 HH:MM
            if len(stamp) >= 16:
                out.append((stamp[:10], stamp[11:16], amount))
        if out:
            return out
    return None


def _curve_for_day(slot_amounts: Dict[int, float]) -> List[float]:
    """某交易日的逐「bar 结束槽位」金额 → 241 点累计曲线（亿元，2 位小数）。

    5 分钟 K 只能给出每根 bar 结束时刻的精确累计值。若把这些值直接前向填充，
    曲线会变成「4 分钟平台 + 1 分钟跳升」的阶梯，图上肉眼就是锯齿/台阶。
    原上游 `tools/build_cum.py` 的口径是「边界处精确值 + 中间线性插值」，
    这里照同一口径还原，保证观感与历史一致：

    1. 槽位 0（09:30）累计为 0；
    2. 每根 bar 的结束槽位 i 上放精确累计值 C_i（到该 bar 为止的当日合计）；
    3. 相邻精确点之间按槽位线性插值 → 每分钟都有增量、没有平台；
    4. 首根 bar 之前（0..首个结束槽位）同样从 0 线性升到 C_first。

    这样曲线严格单调不减、且各段内斜率恒定，与种子里的 1 分钟口径曲线
    平滑度同量级（mean|Δ²| 由 ~6.6 降到 ~0.12）。
    """
    if not slot_amounts:
        return [0.0] * len(SLOTS)

    known: List[Tuple[int, float]] = []
    acc = 0.0
    for i in sorted(slot_amounts):
        acc += slot_amounts[i]
        known.append((i, acc))

    curve = [0.0] * len(SLOTS)

    # 首根 bar 之前的空档：0 → C_first 线性
    first_i, first_v = known[0]
    if first_i > 0:
        for t in range(first_i + 1):
            curve[t] = first_v * t / first_i
    else:
        curve[0] = first_v

    # 相邻精确点之间线性插值
    for (i0, v0), (i1, v1) in zip(known, known[1:]):
        step = i1 - i0
        if step <= 0:
            curve[i1] = v1
            continue
        for t in range(i0 + 1, i1 + 1):
            curve[t] = v0 + (v1 - v0) * (t - i0) / step

    # 末根 bar 之后保持末值（正常收盘 15:00 即最后一根，不会走到这里）
    last_i, last_v = known[-1]
    for t in range(last_i + 1, len(SLOTS)):
        curve[t] = last_v

    return [round(v / 1e8, 2) for v in curve]


def _build_cum_from_bars(bars_by_secid: Dict[str, List[Tuple[str, str, float]]],
                         ok: Optional[int] = None, as_of: Optional[str] = None) -> Optional[dict]:
    """从各标的的 (date, HH:MM, amount) 构造 cumData。

    1. 跨标的按 (date, 槽位) 合计成交额（元），同时统计每日 bar 数；
    2. full_days 要求每只成功 ETF 各有 48 根去重后的有效 bar，并排除当日；
    3. prev_date = full_days[-1]；prev = 当天 241 点（边界精确 + 线性插值）累计曲线；
    4. window = full_days[-21:-1]（prev 之前 20 个交易日），逐日曲线逐点平均 → avg20。
    """
    if not bars_by_secid:
        return None
    if ok is None:
        ok = len(bars_by_secid)

    day_slots: Dict[str, Dict[int, float]] = {}
    day_counts: Dict[str, int] = {}
    cutoff = as_of or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    expected_times = {t for t in SLOTS if t != "09:30" and int(t[3:]) % 5 == 0}
    for bars in bars_by_secid.values():
        seen = set()
        for date, hhmm, amount in bars:
            if date >= cutoff or hhmm not in expected_times or not math.isfinite(amount) or amount < 0:
                continue
            idx = _slot_index(hhmm)
            if idx < 0 or (date, idx) in seen:
                continue
            ds = day_slots.setdefault(date, {})
            ds[idx] = ds.get(idx, 0.0) + amount
            seen.add((date, idx))
        for date, _idx in seen:
            day_counts[date] = day_counts.get(date, 0) + 1

    if not day_slots:
        return None

    # Each fund contributes at most 48 unique bars: total equality implies full coverage.
    threshold = len(expected_times) * ok
    full_days = sorted(d for d in day_slots if day_counts[d] >= threshold)
    if not full_days:
        logger.warning("etf_flow cum: no full days (threshold=%d)", threshold)
        return None

    prev_date = full_days[-1]
    prev = _curve_for_day(day_slots[prev_date])

    window = full_days[-21:-1]  # prev 之前 20 个交易日
    if window:
        curves = [_curve_for_day(day_slots[d]) for d in window]
        avg_days = len(curves)
        avg20 = [round(sum(c[i] for c in curves) / avg_days, 2) for i in range(len(SLOTS))]
        avg_from, avg_to = window[0], window[-1]
    else:
        # 历史不足 20 天：用实际天数；连一天都没有就退化为 prev 当天
        window = [prev_date]
        avg_days = 1
        avg20 = prev[:]
        avg_from = avg_to = prev_date

    return {
        "slots": list(SLOTS),
        "prevDate": prev_date,
        "prev": prev,
        "avgDays": avg_days,
        "avgFrom": avg_from,
        "avgTo": avg_to,
        "avg20": avg20,
        "builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "eastmoney-klt5",
        "buildVersion": BUILD_VERSION,
    }


def build_cum(funds=None, timeout=25.0, workers=8) -> Optional[dict]:
    """构建自建的上一交易日版本 cumData。funds 缺省读 seed 的 20 只 ETF。"""
    if funds is None:
        with open(SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
        funds = seed.get("funds") or []
    # 只用 20 只 ETF，不含 index_secid（指数成交额量级完全不同）
    secids = [f.get("secid") for f in funds if isinstance(f, dict) and f.get("secid")]

    def work(secid):
        return secid, _fetch_kline(secid, timeout=min(timeout, _FETCH_TIMEOUT))

    bars_by_secid: Dict[str, List[Tuple[str, str, float]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, s) for s in secids]):
            secid, bars = fut.result()
            if bars:
                bars_by_secid[secid] = bars

    ok = len(bars_by_secid)
    if ok < _MIN_OK:
        logger.warning("etf_flow cum: fetch ok=%d/%d < %d, skip build", ok, len(secids), _MIN_OK)
        return None

    return _build_cum_from_bars(bars_by_secid, ok=ok)


def load_cum(path=CUM_PATH) -> Optional[dict]:
    """读磁盘缓存；文件缺失/损坏返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("buildVersion") == BUILD_VERSION else None
    except (OSError, ValueError):
        return None


def refresh_cum(now=None, ttl=1800.0) -> Optional[dict]:
    """带 TTL + 单飞的刷新入口。

    - 缓存命中（builtAt 距今 < ttl）→ 直接返回缓存；
    - 否则尝试抢进程内锁（不阻塞、不排队），抢到才重新 build；
    - build 成功先写 .tmp 再 os.replace 原子替换；失败保留旧文件 + 120s 冷却。
    """
    now = now if now is not None else time.time()

    def _age(cached):
        try:
            built_t = cached.get("builtAt", "")
            if not built_t:
                return float("inf")
            return now - time.mktime(time.strptime(built_t, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError, AttributeError):
            return float("inf")

    cached = load_cum()
    if cached and _age(cached) < ttl:
        return cached

    # 冷却：build 失败后 120s 内不再重试
    if _last_fail["ts"] and now - _last_fail["ts"] < 120.0:
        return cached

    if not _lock.acquire(blocking=False):
        # 别的线程正在构建，把旧帧给它返回也够用
        return cached
    try:
        fresh = build_cum()
        if fresh is None:
            _last_fail["ts"] = time.time()
            return cached
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                             dir=os.path.dirname(CUM_PATH), delete=False) as f:
                tmp_path = f.name
                json.dump(fresh, f, ensure_ascii=False)
            os.replace(tmp_path, CUM_PATH)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return fresh
    finally:
        _lock.release()


_lock = threading.Lock()
_last_fail = {"ts": 0.0}
