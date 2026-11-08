# -*- coding: utf-8 -*-
"""ETF 宽基资金观测看板 · 独立版服务端。

只依赖 Flask + requests，无数据库、无 Redis、无站内模块：

    /                        看板页面（静态 HTML + 本地 ECharts）
    /v1/etf_flow/board       历史 island（静态种子 / 可选外部看板）
    /v1/etf_flow/bundle      21 个标的当日分钟线 + 实时快照（一次打包）
    /v1/etf_flow/minute      盘中分钟观测的另一种口径（可选，页面不依赖）
    /v1/etf_flow/seed        扁平化种子数据（二次开发用）
    /v1/etf_flow/diag        缓存 / 数据源自检
    /healthz                 探活

设计要点：浏览器每轮刷新原本要发 22 个 /api/kline|/api/hq 请求，前端装载器
（static/js/etf_flow_board_boot.js）把它们收口到单个 /bundle，服务端按
盘中 60s / 盘外 300s 缓存，无论多少访客，上游每分钟最多被打一轮。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3.util.connection as _urllib3_conn
from flask import Flask, jsonify, render_template, request

from etf_flow.board_data import (
    build_bundle,
    fetch_board_islands,
    islands_from_seed,
    source_health,
)
from etf_flow.cache import BundleCache
from etf_flow.cum import load_cum, refresh_cum

# 东财 IPv6 边缘节点对脚本请求会 TLS 后空响应，进程级强制 IPv4。
_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("etf_flow_board")

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
SEED_PATH = os.path.join(APP_ROOT, "etf_flow", "data", "etf_flow_seed.json")
TRADING_WINDOWS = ((915, 1135), (1255, 1505))
_NODE_JITTER = abs(hash("%s:%s" % (os.getpid(), os.uname().nodename))) % 10
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}
_EM_SESSION = requests.Session()
_EM_SESSION.trust_env = os.getenv("ETF_TRUST_ENV", "0") == "1"

app = Flask(__name__, static_folder="static", template_folder="templates")
if hasattr(app, "json"):
    app.json.ensure_ascii = False
else:  # Flask < 2.3
    app.config["JSON_AS_ASCII"] = False

# ---------------------------------------------------------------- 极简限流
# 零依赖的进程内计数：默认 120 次/分钟/IP，防止公开部署时被刷爆上游。
# 多实例部署请在网关层做全局限流，这里的计数不跨进程共享。
_RATE_LIMIT = int(os.getenv("ETF_RATE_LIMIT_PER_MIN") or "120")
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


@app.before_request
def _guard():
    if not request.path.startswith("/v1/"):
        return None
    if _RATE_LIMIT <= 0:
        return None
    ip = request.remote_addr or "-"
    now = time.time()
    with _hits_lock:
        bucket = [t for t in _hits.get(ip, []) if now - t < 60]
        bucket.append(now)
        _hits[ip] = bucket
        if len(bucket) > _RATE_LIMIT:
            return jsonify({"code": 42900, "message": "请求过于频繁", "data": None}), 429
    return None


def _success(data=None, message="success"):
    return jsonify({"message": message, "data": data, "code": 20000})


def _fail(message="fail", code=400):
    # 业务失败统一 HTTP 200，避免监控把上游抖动计成 5xx
    return jsonify({"message": message, "data": None, "code": code})


# ---------------------------------------------------------------- 种子 / island
_seed_cache = {"mtime": 0, "data": None}
_islands_cache = {"ts": 0.0, "data": None}


def _load_seed():
    mtime = os.path.getmtime(SEED_PATH)
    if _seed_cache["mtime"] != mtime:
        with open(SEED_PATH, encoding="utf-8") as f:
            _seed_cache["data"] = json.load(f)
        _seed_cache["mtime"] = mtime
    return _seed_cache["data"]


def _apply_fresh_cum(islands):
    """用自建的上一交易日 / 近 20 日均值覆盖种子里冻结的 cumData。"""
    out = dict(islands)
    try:
        fresh = refresh_cum()
    except Exception:
        logger.warning("fresh cum failed, keep seed cumData", exc_info=True)
        return out
    if fresh:
        out["cumData"] = fresh
    return out


def _board_islands():
    """优先拉可选外部看板的 island 全量；不可达时用仓库自带种子拼同构兜底。"""
    now = time.time()
    if _islands_cache["data"] and now - _islands_cache["ts"] < 600:
        return _apply_fresh_cum(dict(_islands_cache["data"]))
    islands = fetch_board_islands()
    if not islands:
        islands = islands_from_seed(_load_seed())
        _islands_cache["data"] = islands
        _islands_cache["ts"] = now
    else:
        _islands_cache["data"] = islands
        _islands_cache["ts"] = now
    return _apply_fresh_cum(dict(islands))


# ---------------------------------------------------------------- bundle（盘中行情）
_BUNDLE_DISK_PATH = os.getenv("ETF_BUNDLE_CACHE") or os.path.join(
    tempfile.gettempdir(), "etf_flow_bundle.json"
)


def _build_bundle_now():
    seed = _load_seed()
    data = build_bundle(
        seed.get("funds") or [],
        seed.get("index_secid") or "1.000001",
    )
    return data if data.get("kline") else None


def _log_source_cooling(data):
    cooling = [k for k, v in (data.get("sources_health") or {}).items() if v.get("cooling")]
    if cooling:
        logger.warning("行情源熔断中: %s（本轮 %s）", ",".join(cooling), data.get("source"))


def _bundle_ttl():
    """盘中 60s + 按进程固定抖动；盘外 300s。"""
    lt = time.localtime()
    now = lt.tm_hour * 100 + lt.tm_min
    if lt.tm_wday < 5 and any(a <= now <= b for a, b in TRADING_WINDOWS):
        return 60 + _NODE_JITTER
    return 300


_bundle_cache = BundleCache(
    builder=_build_bundle_now,
    ttl=_bundle_ttl,
    disk_path=_BUNDLE_DISK_PATH,
    on_success=_log_source_cooling,
)


# ---------------------------------------------------------------- 路由
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/v1/etf_flow/board")
def board():
    try:
        return _success(_board_islands())
    except FileNotFoundError:
        return _fail("种子文件不存在：etf_flow/data/etf_flow_seed.json", code=404)
    except Exception as e:
        logger.exception("board failed")
        return _fail("看板基础数据读取失败: %s" % e, code=500)


@app.route("/v1/etf_flow/bundle")
def bundle():
    try:
        data, meta = _bundle_cache.get()
        out = dict(data or {})
        out.update({k: v for k, v in (meta or {}).items() if v is not None})
        return _success(out)
    except Exception as e:
        logger.exception("bundle failed")
        return _fail("行情打包拉取失败: %s" % e, code=500)


@app.route("/v1/etf_flow/seed")
def seed():
    """扁平化种子；接了外部看板时合并其历史序列。"""
    try:
        data = dict(_load_seed())
        islands = fetch_board_islands()
        if islands:
            cmp_ = islands.get("cmpData") or {}
            data.update({
                "daily": islands.get("dailyData") or data.get("daily"),
                "recent": cmp_.get("recent") or data.get("recent"),
                "support": cmp_.get("support") or data.get("support"),
                "excluded": cmp_.get("excluded") or data.get("excluded"),
                "typ": islands.get("typData") or data.get("typ"),
                "broad": islands.get("broadData") or data.get("broad"),
                "cum": islands.get("cumData") or data.get("cum"),
            })
            table = ((islands.get("initData") or {}).get("table") or [])
            if table:
                data["funds"] = [
                    {"code": t["code"], "name": t["name"],
                     "secid": ("0." if t["code"].startswith("1") else "1.") + t["code"]}
                    for t in table
                ]
        meta = dict(data.get("meta") or {})
        meta["served_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["meta"] = meta
        return _success(data)
    except Exception as e:
        logger.exception("seed failed")
        return _fail("种子数据读取失败: %s" % e, code=500)


_minute_cache = {"ts": 0.0, "data": None}


def _minute_ttl():
    lt = time.localtime()
    now = lt.tm_hour * 100 + lt.tm_min
    if lt.tm_wday < 5 and any(a <= now <= b for a, b in TRADING_WINDOWS):
        return 60
    return 1800


def _fetch_trends(secid: str):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        "?secid=%s&ndays=1&iscr=0&ut=%s"
        "&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58" % (secid, _UT)
    )
    try:
        r = _EM_SESSION.get(url, headers=_EM_HEADERS, timeout=6)
        data = (r.json() or {}).get("data") or {}
        rows = []
        for line in data.get("trends") or []:
            p = str(line).split(",")
            if len(p) < 7:
                continue
            try:
                rows.append((p[0][11:16], float(p[2]), float(p[6])))
            except (ValueError, IndexError):
                continue
        return {"preClose": data.get("preClose"), "rows": rows}
    except Exception:
        logger.warning("trends2 failed secid=%s", secid, exc_info=True)
        return None


@app.route("/v1/etf_flow/minute")
def minute():
    """20 只 ETF 每分钟成交额加总 + 上证综指分时（页面用 /bundle，此接口备用）。"""
    force = str(request.args.get("force_refresh") or "").lower() in ("1", "true", "yes")
    try:
        now = time.time()
        if not force and _minute_cache["data"] and now - _minute_cache["ts"] < _minute_ttl():
            data = dict(_minute_cache["data"])
            data["cached"] = True
            return _success(data)

        seed = _load_seed()
        funds = seed["funds"]
        secids = [f["secid"] for f in funds] + [seed.get("index_secid", "1.000001")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            trends = list(pool.map(_fetch_trends, secids))
        idx_map = {t: (c, a) for t, c, a in (trends[-1] or {}).get("rows", [])}
        etf_maps = [{t: a for t, c, a in (tr or {}).get("rows", [])} for tr in trends[:len(funds)]]
        times, amt_total, idx_close = [], [], []
        for t, (close, _a) in idx_map.items():
            times.append(t)
            amt_total.append(round(sum(m.get(t, 0.0) for m in etf_maps) / 1e8, 2))
            idx_close.append(close)

        table = []
        for f, tr in zip(funds, trends[:len(funds)]):
            rows = (tr or {}).get("rows") or []
            pre = (tr or {}).get("preClose")
            if not rows or not pre:
                table.append({"code": f["code"], "name": f["name"],
                              "price": None, "pct": None, "amount_yi": None})
                continue
            close = rows[-1][1]
            amount = sum(r[2] for r in rows)
            table.append({"code": f["code"], "name": f["name"],
                          "price": round(close, 3),
                          "pct": round((close / pre - 1) * 100, 2),
                          "amount_yi": round(amount / 1e8, 2)})

        data = {
            "date": time.strftime("%Y-%m-%d"),
            "times": times, "amount_yi": amt_total, "idx_close": idx_close,
            "idx_pre_close": (trends[-1] or {}).get("preClose"),
            "table": sorted(table, key=lambda x: -(x.get("amount_yi") or 0)),
            "total_amount_yi": round(sum(t["amount_yi"] or 0 for t in table), 2),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if data["times"]:
            _minute_cache.update(ts=now, data=data)
            data["cached"] = False
            return _success(data)
        if _minute_cache["data"]:
            stale = dict(_minute_cache["data"])
            stale.update(cached=True, stale=True)
            return _success(stale)
        return _success(data)
    except Exception as e:
        logger.exception("minute failed")
        return _fail("分钟数据拉取失败: %s" % e, code=500)


@app.route("/v1/etf_flow/diag")
def diag():
    cache = _bundle_cache.diag({
        "node": "%s:%s" % (os.uname().nodename, os.getpid()),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": source_health(),
        "rate_limit_per_min": _RATE_LIMIT,
    })
    frame = _bundle_cache.peek() or {}
    cache["bundle"] = {
        "built_at": frame.get("built_at"),
        "source": frame.get("source"),
        "symbols": len(frame.get("kline") or {}),
        "missing": frame.get("missing") or [],
    }
    cum = load_cum() or {}
    cache["cum"] = {
        "prevDate": cum.get("prevDate"),
        "builtAt": cum.get("builtAt"),
        "source": cum.get("source"),
    }
    return _success(cache)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        threaded=True,
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
