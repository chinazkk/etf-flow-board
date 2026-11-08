# -*- coding: utf-8 -*-
"""ETF 看板 bundle 的三级缓存：进程内 → 落盘共享 → 上游。

这一层只做一件事：**让用户请求永远不等上游**。取数逻辑（三源分片、熔断、
时间轴对齐）全在 etf_flow_board_data 里，这里只负责「什么时候允许打上游」：

  * TTL 内                 → 直接返回内存帧，零成本；
  * 过期但仍在宽限期内     → 先把旧帧给页面，刷新丢给后台单飞线程；
  * 超过宽限 / 冷启动      → 同步取一次，宁可慢也要给真数据；
  * 刚失败过（冷却期）     → 一次都不打上游，只回最后一帧并标记 stale。

「旧帧」不是妥协：分钟线是当日累计，差两三分钟不影响看资金节奏，而一轮上游
要 2-4 秒 —— 让用户盯着转圈才是更糟的选择。真正过期太久（超过宽限）时该等就等。

落盘缓存解决的是多 worker 问题：gunicorn 每个 worker 各有内存，不共享就会
各自 21 个请求把上游频控撞出来。谁先拉到谁写盘，其余读盘。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 60.0
DEFAULT_GRACE_S = 180.0      # 过期后还能直接回旧帧的宽限
DEFAULT_FAIL_COOLDOWN_S = 20.0
# 旧帧的兜底期限：超过这个年龄就不再用旧帧糊弄，宁可让用户等一次真取数。
DEFAULT_HARD_MAX_S = 3600.0
# 旧到这个程度才在页面上标 stale（黄条）。3-10 分钟的旧帧还在后台刷新，
# 弹提示只会让人以为出故障 —— 页脚的更新时间已经说明了一切。
DEFAULT_STALE_AFTER_S = 600.0


class BundleCache:
    """bundle 缓存。builder 无参、成功返回 dict、失败返回 None。"""

    def __init__(self, builder: Callable[[], Optional[Dict[str, Any]]],
                 ttl: Any = DEFAULT_TTL_S,
                 grace: float = DEFAULT_GRACE_S,
                 fail_cooldown: float = DEFAULT_FAIL_COOLDOWN_S,
                 disk_path: Optional[str] = None,
                 on_success: Optional[Callable[[Dict[str, Any]], None]] = None,
                 hard_max: float = DEFAULT_HARD_MAX_S,
                 stale_after: float = DEFAULT_STALE_AFTER_S):
        self._builder = builder
        self._ttl = ttl if callable(ttl) else (lambda: float(ttl))
        self._grace = grace
        self._fail_cooldown = fail_cooldown
        self._hard_max = hard_max
        self._stale_after = stale_after
        self._disk_path = disk_path
        self._on_success = on_success

        self._lock = threading.Lock()
        self._mem: Dict[str, Any] = {"ts": 0.0, "data": None}
        self._fail_ts = 0.0
        self._state = {"running": False, "started_at": 0.0,
                       "ok_at": 0.0, "fail_at": 0.0, "count": 0, "last_ms": 0}

    # ---------------- 对外 ----------------

    def ttl(self) -> float:
        return float(self._ttl())

    def get(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """返回 (帧数据, 元信息)。元信息里 cached/stale/refreshing 给页面做提示用。"""
        now = time.time()
        ttl = self.ttl()

        # 1) 内存还新鲜
        if self._mem["data"] is not None and now - self._mem["ts"] < ttl:
            return self._mem["data"], {"cached": True}

        # 2) 同机其它 worker 刚写过盘，直接复用（一个 TTL 窗口只打一轮上游）
        shared = self._load_disk(ttl)
        if shared is not None:
            # 时间戳取盘文件的 mtime 而不是 now：否则每交接一次就多算一轮 TTL，
            # 多个 worker 轮流读盘会把「60 秒新鲜」滚成两三分钟。
            self._mem["ts"] = self._disk_mtime() or now
            self._mem["data"] = shared
            return shared, {"cached": True}

        # 3) 过期但在宽限内：旧帧立刻回，刷新交给后台
        stale = self._mem["data"]
        if stale is None:
            stale = self._load_disk()
        age = self._age(stale)
        if stale is not None and age <= self._grace:
            spawned = self._spawn()
            return stale, {"cached": True, "refreshing": spawned}

        # 3b) 过了宽限但这一帧还没到「该怀疑它」的年龄：照样先回，后台刷新。
        #     这段是为多机准备的 —— nginx 走 least_conn，某台节点可能几分钟没被
        #     访问，缓存一冷就要在现场等 7 秒。为一份仍属当日的分钟线让用户干等，
        #     不划算；页脚的更新时间 + 后台刷新已经足够诚实。
        if stale is not None and age <= self._hard_max:
            spawned = self._spawn()
            meta = {"cached": True, "refreshing": spawned}
            if age > self._stale_after:
                meta["stale"] = True
            return stale, meta

        # 4) 冷却期内不再打上游，免得把频控越撞越死
        if stale is not None and now - self._fail_ts < self._fail_cooldown:
            return stale, {"cached": True, "stale": True}

        # 5) 冷启动 / 过期太久：同步取一次
        data = self._build()
        if data is not None:
            return data, {"cached": False}

        # 6) 三路都挂了：回最后一帧，并明说这是什么时候的数据
        stale = self._mem["data"] or self._load_disk()
        if stale is not None:
            return stale, {"cached": True, "stale": True}
        return {"kline": {}, "hq": {}, "missing": []}, {"cached": False, "stale": True}

    def peek(self) -> Dict[str, Any]:
        """当前那一帧（内存优先，其次落盘），绝不触发取数。"""
        return self._mem["data"] or self._load_disk() or {}

    def diag(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """纯内存快照，绝不触发取数。给探活/巡检用。"""
        now = time.time()
        data = self._mem["data"] or self._load_disk() or {}
        age = self._age(data)
        out = {
            "ttl_s": self.ttl(),
            "grace_s": self._grace,
            "cache_age_s": None if age == float("inf") else int(round(age)),
            "cache_channel": "memory" if self._mem["data"] is not None
                             else ("disk" if age != float("inf") else "none"),
            "cached_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - age))
                          if age != float("inf") else None),
            "refresh": {
                "running": self._state["running"],
                "count": self._state["count"],
                "last_ms": self._state["last_ms"],
                "ok_at": _fmt(self._state["ok_at"]),
                "fail_at": _fmt(self._state["fail_at"]),
            },
        }
        if extra:
            out.update(extra)
        return out

    def reset(self) -> None:
        """清空缓存与状态（单测用）。"""
        with self._lock:
            self._mem.update(ts=0.0, data=None)
            self._fail_ts = 0.0
            self._state.update(running=False, started_at=0.0,
                               ok_at=0.0, fail_at=0.0, count=0, last_ms=0)

    # ---------------- 内部 ----------------

    def _age(self, data) -> float:
        """那一帧有多旧。内存帧按写入时间；盘帧按文件 mtime。"""
        if data is None:
            return float("inf")
        if self._mem["data"] is not None and data is self._mem["data"]:
            return time.time() - self._mem["ts"]
        try:
            return time.time() - os.path.getmtime(self._disk_path)
        except (OSError, TypeError):
            return float("inf")

    def _disk_mtime(self) -> Optional[float]:
        try:
            return os.path.getmtime(self._disk_path)
        except (OSError, TypeError):
            return None

    def _load_disk(self, max_age: Optional[float] = None):
        if not self._disk_path:
            return None
        try:
            if max_age is not None and time.time() - os.path.getmtime(self._disk_path) > max_age:
                return None
            with open(self._disk_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("kline"):
                return data
        except (OSError, ValueError):
            pass
        return None

    def _store_disk(self, data) -> None:
        if not self._disk_path:
            return
        try:
            tmp = self._disk_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._disk_path)
        except OSError:
            logger.warning("etf_flow bundle 落盘缓存写入失败", exc_info=True)

    def _remember(self, data) -> None:
        self._mem["ts"] = time.time()
        self._mem["data"] = data
        self._fail_ts = 0.0
        self._state["ok_at"] = time.time()
        self._state["count"] += 1
        self._store_disk(data)

    def _build(self):
        started = time.time()
        try:
            data = self._builder()
        except Exception:
            logger.warning("etf_flow bundle 取数异常", exc_info=True)
            data = None
        self._state["last_ms"] = int((time.time() - started) * 1000)
        if data:
            self._remember(data)
            if self._on_success:
                try:
                    self._on_success(data)
                except Exception:
                    logger.debug("on_success 回调失败", exc_info=True)
            return data
        self._fail_ts = time.time()
        self._state["fail_at"] = time.time()
        return None

    def _worker(self) -> None:
        try:
            self._build()
        finally:
            with self._lock:
                self._state["running"] = False

    def _spawn(self) -> bool:
        """起后台刷新；已在跑、或刚失败还在冷却，返回 False。"""
        with self._lock:
            if self._state["running"]:
                return False
            if time.time() - self._fail_ts < self._fail_cooldown:
                return False
            self._state["running"] = True
            self._state["started_at"] = time.time()
        threading.Thread(target=self._worker, name="etf-flow-refresh", daemon=True).start()
        return True


def _fmt(ts: float) -> Optional[str]:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None
