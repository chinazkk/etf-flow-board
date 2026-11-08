/* 看板渲染层：逐字沿用原版看板 app.js（2026-11-03 复刻）。
   数据仍从 window.__ISLANDS 取，取数改由 etf_flow_board_boot.js 在 fetch 层代理，
   因此本文件与原版保持逐字一致 —— 上游改版时直接整份替换即可。 */
/* 逻辑层：分钟观测 + 托底信号 + 日度全景 + 放量案例（原生JS，无框架无构建） */
(function () {
  "use strict";

  /* 2026-10-15 换仓后的新篮子：沪深300/科创50/创业板/中证500/中证1000/中证A500/上证50 等
     按用户给定名单，顺序即重要性顺序（成交额降序）。m: "1"=上交所(sh)、"0"=深交所(sz)。 */
  var ETFS = [
    { code: "510310", m: "1" }, { code: "588000", m: "1" }, { code: "159915", m: "0" },
    { code: "510300", m: "1" }, { code: "563360", m: "1" }, { code: "159352", m: "0" },
    { code: "512100", m: "1" }, { code: "159338", m: "0" }, { code: "510500", m: "1" },
    { code: "159949", m: "0" }, { code: "588080", m: "1" }, { code: "510050", m: "1" },
    { code: "510210", m: "1" }, { code: "159845", m: "0" }, { code: "159952", m: "0" },
    { code: "159361", m: "0" }, { code: "512050", m: "1" }, { code: "159633", m: "0" },
    { code: "560010", m: "1" }, { code: "159919", m: "0" }
  ];
  var INDEX_SECID = "1.000001";
  var REFRESH_TRADING = 60, REFRESH_IDLE = 300;
  var BAR_LABEL = "20只ETF分钟成交额合计", IDX_LABEL = "上证综指";
  /* 左轴（分钟成交额）上限，单位亿元。
     两难：开盘集合竞价的第一分钟常有 15~40 亿的脉冲，若按它定标，盘中真正有意义的
     放量变化会被压成一条平线；但若只按「剔除开盘后的峰值」定标，开盘那根就会超出上限
     被 ECharts 截断、顶满整个图 —— 顶格的一根柱子比「高」本身更碍眼，而且读不出真实值。

     折中：两条线同时算——
       core = 剔除开盘前 OPEN_IGNORE 根之后的峰值（衡量「盘中常态量能」）
       peak = 全时段峰值（含开盘脉冲）
     默认按 core 定档；只有当 peak 超出该档位时才抬到能容纳 peak 的档位，
     且不允许超过 core 档位的 AXIS_CAP_X 倍（防止极端脉冲把整根轴拉到天上）。
     档位都取 5 的倍数，配合 splitNumber:5 得到整数刻度。 */
  var BAR_AXIS_MIN = 10;
  var OPEN_IGNORE = 10;                       // 忽略开盘前 10 根（09:30–09:39）的竞价脉冲
  var AXIS_CAP_X = 2.5;                       // 轴最多抬到「盘中常态档位」的 2.5 倍
  var AXIS_STEPS = [10, 15, 20, 25, 30, 40, 50, 60, 80];
  function axisStep(p) {
    for (var s = 0; s < AXIS_STEPS.length; s++) {
      if (AXIS_STEPS[s] >= p) return AXIS_STEPS[s];
    }
    return Math.ceil(p / 20) * 20;
  }
  function barAxisMax(bars) {
    var core = 0, peak = 0;
    for (var i = 0; i < bars.length; i++) {
      var v = Number(bars[i]);
      if (isNaN(v) || v <= 0) continue;
      if (v > peak) peak = v;
      if (i >= OPEN_IGNORE && v > core) core = v;
    }
    var base = Math.max(axisStep(core), BAR_AXIS_MIN);
    if (peak > base) {
      /* 封顶也走档位，保证刻度仍是 5 的整数倍（否则 splitNumber:5 会切出 7.6 这种刻度） */
      var cap = axisStep(base * AXIS_CAP_X);
      return Math.max(base, Math.min(axisStep(peak), cap));
    }
    return base;
  }

  var state = { paused: false, remain: 1, loading: false };
  var chart = null;
  function ensureChart() {
    if (!chart) chart = echarts.init(document.getElementById("chart"));
    return chart;
  }
  function on(el, ev, fn) { if (el && typeof el.addEventListener === "function") el.addEventListener(ev, fn, false); }

  var initData = (window.__ISLANDS && window.__ISLANDS.initData) || {};

  /* 统一的 ETF 名称：以东方财富（Choice 同源）的基金简称为唯一准绳。
     实时行情接口返回的是交易所/新浪的场内简称（如「创业板」「300ETF」），
     会破坏「指数 + ETF + 管理人」的统一命名，因此一律以 NAME_MAP 为准。 */
  var NAME_MAP = {};
  (initData.table || []).forEach(function (r) { if (r.code) NAME_MAP[r.code] = r.name; });
  function etfName(code, fallback) {
    return NAME_MAP[code] || fallback || code;
  }

  var cum = [], preClose = Number(initData.preClose) || null;
  var minuteTimes = [], minuteBars = [], minuteCloses = [];
  /* 当前 minuteBars 属于哪个交易日 —— 三线图要靠它判断「今日线」是不是真的今天的。
     开盘前/周末时行情接口最新一根还是上一交易日，若不判断，今日线会画出昨天同一条曲线，
     与「昨日线」完全重合（两根线叠在一起，看着像画错了）。 */
  var minuteDate = String(initData.date || "");

  function cls(v, base) { return (v > 0 ? "up" : v < 0 ? "down" : (base || "")); }

  function buildCum(bars) {
    cum = [];
    var acc = 0;
    bars.forEach(function (b) { acc += b; cum.push(+acc.toFixed(2)); });
  }

  function fullAxis() {
    var ts = [];
    var segs = [["09:30", 121], ["13:01", 120]];
    segs.forEach(function (seg) {
      var start = seg[0], count = seg[1];
      var hh = parseInt(start.slice(0, 2), 10), mm = parseInt(start.slice(3), 10);
      for (var i = 0; i < count; i++) {
        var t = ("0" + hh).slice(-2) + ":" + ("0" + mm).slice(-2);
        ts.push(t);
        mm += 1;
        if (mm === 60) { mm = 0; hh += 1; }
      }
    });
    return ts;
  }
  var FULL_T = fullAxis();

  function alignToFull(times, bars, closes) {
    var map = {};
    times.forEach(function (t, i) { map[t] = i; });
    var fb = [], fc = [];
    FULL_T.forEach(function (t) {
      if (map[t] != null) {
        fb.push(bars[map[t]]);
        fc.push(closes[map[t]]);
      } else {
        fb.push(null);
        fc.push(null);
      }
    });
    return { bars: fb, closes: fc };
  }

  function pctBg(p, absMax) {
    if (!absMax) return "";
    var t = Math.abs(p) / absMax;
    if (t > 1) t = 1;
    var a = 0.10 + 0.58 * t;
    var rgb = p > 0 ? "217,54,48" : (p < 0 ? "10,160,110" : "120,130,145");
    return "background:rgba(" + rgb + "," + a.toFixed(3) + ");";
  }

  function renderTable(sorted) {
    var maxA = 0;
    sorted.forEach(function (r) { if (r.amount > maxA) maxA = r.amount; });
    var total = 0;
    sorted.forEach(function (r) { total += r.amount; });
    var absMax = 0;
    sorted.forEach(function (r) { if (Math.abs(r.pct) > absMax) absMax = Math.abs(r.pct); });
    var tb = document.getElementById("etfTable");
    var html = "";
    sorted.forEach(function (r) {
      var share = total > 0 ? (r.amount / total * 100) : 0;
      var w = maxA > 0 ? (r.amount / maxA * 100) : 0;
      html += "<tr><td>" + r.code + "</td><td>" + r.name + "</td>" +
        "<td class='num amt-cell'><span class='amt-bg' style='width:" + w.toFixed(1) + "%'></span><span class='amt-val'>" + r.amount.toFixed(2) + "</span></td>" +
        "<td class='num'>" + share.toFixed(1) + "%</td>" +
        "<td class='num pct-cell' style='" + pctBg(r.pct, absMax) + "'>" + (r.pct > 0 ? "+" : "") + r.pct.toFixed(2) + "%</td></tr>";
    });
    tb.innerHTML = html;
  }

  function renderStatic() {
    var t = initData.times || [], b = initData.bars || [], c = initData.closes || [], tb = initData.table || [];
    if (!t.length) return;
    minuteTimes = t; minuteBars = b; minuteCloses = c;
    minuteDate = String(initData.date || "");
    buildCum(b);
    document.getElementById("tagDate").textContent = (initData.date || "") + " 分时";
    renderTable(tb.slice().sort(function (x, y) { return y.amount - x.amount; }));
    drawMinuteChart();
    drawCumChart();
    document.getElementById("tagUpdate").textContent = "已载入收盘数据";
    document.getElementById("tagUpdate").className = "tag ok";
  }

  function drawMinuteChart() {
    var al = alignToFull(minuteTimes, minuteBars, minuteCloses);
    var shownBars = al.bars;
    var shownCloses = al.closes;
    var shownT = FULL_T;
    var lineColor = "#e8862e";
    var mark = preClose ? {
      symbol: "none",
      lineStyle: { type: "dashed", width: 1, color: "#8a93a3" },
      label: { formatter: "昨收 " + preClose.toFixed(2), position: "insideEndTop" },
      data: [{ yAxis: preClose }]
    } : undefined;
    ensureChart().setOption({
      title: { text: "20只ETF分钟成交额与上证综指分时", left: 8, top: 6, padding: 0, textStyle: { fontSize: 14 } },
      legend: { data: [BAR_LABEL, IDX_LABEL], left: 8, top: 26, type: "scroll", height: 24, itemWidth: 16, itemHeight: 10 },
      grid: { top: 76, left: 16, right: 8, bottom: 54, containLabel: true },
      tooltip: { trigger: "axis", formatter: minuteTooltip },
      xAxis: { type: "category", data: shownT, axisLabel: { hideOverlap: true }, boundaryGap: true },
      yAxis: [
        { type: "value", name: "亿元", min: 0, max: barAxisMax(shownBars), splitNumber: 5, nameTextStyle: { align: "left" } },
        { type: "value", name: "上证综指（点）", scale: true, nameTextStyle: { align: "right" } }
      ],
      /* moveOnMouseMove:false —— 让左键拖动留给区间框选，而不是 dataZoom 平移 */
      dataZoom: [ { type: "inside", xAxisIndex: 0, moveOnMouseMove: false, zoomOnMouseWheel: true },
                  { type: "slider", xAxisIndex: 0, height: 16, bottom: 12 } ],
      series: [
        { name: BAR_LABEL, type: "bar", data: shownBars },
        { name: IDX_LABEL, type: "line", yAxisIndex: 1, data: shownCloses, connectNulls: false,
          showSymbol: false, lineStyle: { width: 1.6, color: lineColor }, itemStyle: { color: lineColor },
          markLine: mark }
      ]
    }, true);
  }

  /* ---------- 分钟累计成交额三线图 ----------
     今日线直接用页面已有的 buildCum()（第 1 张图的分钟 bars 累加），随刷新同步；
     昨日线与「近20日均值线」由 tools/build_cum.py 每日预生成（新浪 1 分钟 K 只能回溯约 7 个
     交易日，凑不出 20 天，故历史两条线改用 5 分钟 K 计算，边界处为精确值、中间线性插值）。 */
  var cumData = (window.__ISLANDS && window.__ISLANDS.cumData) || {};
  var cumChart = null;
  function ensureCumChart() {
    var el = document.getElementById("chartCum");
    if (!el) return null;
    if (!cumChart) cumChart = echarts.init(el);
    return cumChart;
  }
  /* 累计数组对齐到 241 点横轴；尚未走到的时点留 null，线就自然停在当前分钟 */
  function alignCum(times, arr) {
    var map = {};
    times.forEach(function (t, i) { map[t] = i; });
    return FULL_T.map(function (t) {
      var i = map[t];
      return i == null ? null : arr[i];
    });
  }
  function drawCumChart() {
    var c = ensureCumChart();
    if (!c) return;
    /* 只有分钟数据确实是「今天」的，才画今日线。
       开盘前（9:30 之前）与周末，行情接口最新一根仍是上一交易日的数据，
       此时 minuteBars 与「昨日线」是同一份数据 —— 画出来两条线会完全重合，
       看起来像黄线和红线叠在一起。宁可少画一条，也不画一条假的「今日」。 */
    var d = new Date();
    var nowStr = d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" +
                 ("0" + d.getDate()).slice(-2);
    var isRealToday = !!minuteDate && minuteDate === nowStr;
    var todayLine = (isRealToday && minuteBars.length) ? alignCum(minuteTimes, cum) : [];
    var prevLine = cumData.prev || [];
    var avgLine = cumData.avg20 || [];
    var nAvg = cumData.avgDays || 20;
    var s = [];
    if (todayLine.length) {
      s.push({ name: "今日", type: "line", data: todayLine, connectNulls: false,
               showSymbol: false, lineStyle: { width: 2.4, color: "#d93630" },
               itemStyle: { color: "#d93630" } });
    }
    if (prevLine.length) {
      s.push({ name: (cumData.prevDate || "昨日"), type: "line", data: prevLine, connectNulls: false,
               showSymbol: false, lineStyle: { width: 1.6, color: "#e8862e" },
               itemStyle: { color: "#e8862e" } });
    }
    if (avgLine.length) {
      s.push({ name: "近" + nAvg + "日均值", type: "line", data: avgLine, connectNulls: false,
               showSymbol: false, lineStyle: { width: 1.6, color: "#4c7bf3", type: "dashed" },
               itemStyle: { color: "#4c7bf3" } });
    }
    if (!s.length) return;
    c.setOption({
      title: { text: "20只ETF当日分钟累计成交额（亿元）", left: 8, top: 6, padding: 0,
               textStyle: { fontSize: 14 } },
      legend: { data: s.map(function (x) { return x.name; }), left: 8, top: 26,
                type: "scroll", height: 24, itemWidth: 16, itemHeight: 10 },
      grid: { top: 76, left: 16, right: 8, bottom: 54, containLabel: true },
      tooltip: { trigger: "axis", formatter: function (ps) {
        var out = [ps[0].axisValue];
        ps.forEach(function (p) {
          out.push(p.seriesName + "：" + (p.value == null ? "--" : Number(p.value).toFixed(2) + " 亿"));
        });
        return out.join("<br/>");
      } },
      xAxis: { type: "category", data: FULL_T, axisLabel: { hideOverlap: true }, boundaryGap: false },
      yAxis: { type: "value", name: "累计成交额（亿元）", min: 0, scale: false,
               nameTextStyle: { align: "left" } },
      dataZoom: [ { type: "inside", xAxisIndex: 0 }, { type: "slider", xAxisIndex: 0, height: 16, bottom: 12 } ],
      series: s
    }, true);
  }

  function minuteTooltip(ps) {
    var t = ps[0].axisValue, out = [t];
    var i = FULL_T.indexOf(t);
    ps.forEach(function (p) {
      if (p.seriesType === "bar") { out.push("ETF分钟成交额：" + (p.value == null ? "--" : Number(p.value).toFixed(2) + " 亿")); }
      else { out.push("上证综指：" + (p.value == null ? "--" : Number(p.value).toFixed(2))); }
    });
    if (i != null && cum[i] != null) out.push("累计（截至该分钟）：" + cum[i].toFixed(2) + " 亿");
    if (preClose && ps[1] && ps[1].value != null) {
      out.push("较昨收：" + ((Number(ps[1].value) / preClose - 1) * 100).toFixed(2) + "%");
    }
    return out.join("<br/>");
  }

  /* ---------- JSONP ---------- */
  var seq = 0;
  function jsonp(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var cb = "__mcb" + (++seq) + "_" + Date.now();
      var s = document.createElement("script");
      var timer = setTimeout(function () { cleanup(); reject(new Error("timeout")); }, timeoutMs || 12000);
      function cleanup() { clearTimeout(timer); try { delete window[cb]; } catch (e) { window[cb] = undefined; } if (s.parentNode) s.parentNode.removeChild(s); }
      window[cb] = function (data) { cleanup(); resolve(data); };
      s.onerror = function () { cleanup(); reject(new Error("network error")); };
      s.src = url + (url.indexOf("?") >= 0 ? "&" : "?") + "cb=" + cb;
      (document.head || document.body).appendChild(s);
    });
  }

  function sym(secid) {
    var p = String(secid).split(".");
    return (p[0] === "1" ? "sh" : "sz") + p[1];
  }

  function trends(secid) {
    var u = "/api/kline?symbol=" + sym(secid) + "&scale=1&datalen=260";
    return fetch(u).then(function (r) { return r.json(); }).then(function (arr) {
      var rows = [], lastDay = null;
      (arr || []).forEach(function (k) { if (k && k.day) lastDay = k.day.slice(0, 10); });
      (arr || []).forEach(function (k) {
        if (!k || !k.day || !lastDay || k.day.slice(0, 10) !== lastDay) return;
        if (!isFinite(parseFloat(k.close))) return;
        rows.push({ t: k.day.slice(11, 16), dt: k.day, close: parseFloat(k.close), amount: parseFloat(k.amount) });
      });
      return { pre: null, rows: rows };
    });
  }

  function snapshot() {
    var codes = [INDEX_SECID].concat(ETFS.map(function (e) { return e.m + "." + e.code; })).map(sym).join(",");
    var u = "/api/hq?codes=" + codes;
    return fetch(u).then(function (r) { return r.json(); }).then(function (r) {
      var diff = (r && r.data && r.data.diff) || [];
      var idx = null, list = [];
      diff.forEach(function (x) {
        if (x.f12 === "000001" && idx === null) { idx = x; return; }
        list.push(x);
      });
      return { idx: idx, list: list };
    });
  }

  function renderLive(indexTr, etfTrs, snap) {
    var idxRows = indexTr.rows;
    if (!idxRows.length) {
      document.getElementById("tagUpdate").textContent = "暂无分时数据";
      document.getElementById("tagUpdate").className = "tag err";
      return;
    }
    document.getElementById("tagDate").textContent = (idxRows[0].dt || "").slice(0, 10) + " 分时";
    var sums = {};
    etfTrs.forEach(function (tr) {
      tr.rows.forEach(function (r) { sums[r.t] = (sums[r.t] || 0) + r.amount; });
    });
    var times = [], bars = [], closes = [];
    idxRows.forEach(function (r) {
      times.push(r.t);
      bars.push(+((sums[r.t] || 0) / 1e8).toFixed(2));
      closes.push(r.close);
    });
    minuteTimes = times; minuteBars = bars; minuteCloses = closes;
    minuteDate = String((idxRows[0] && idxRows[0].dt || "").slice(0, 10));
    buildCum(bars);
    if (snap && snap.idx && Number(snap.idx.f18)) preClose = Number(snap.idx.f18);

    if (snap && snap.list) {
      var rows = snap.list.map(function (x) {
        return { code: x.f12, name: etfName(x.f12, x.f14), price: Number(x.f2), pct: Number(x.f3), amount: Number(x.f6) / 1e8 };
      }).sort(function (a, b) { return b.amount - a.amount; });
      renderTable(rows);
    }
    drawMinuteChart();
    drawCumChart();
    document.getElementById("tagUpdate").textContent = "更新于 " + new Date().toTimeString().slice(0, 8);
    document.getElementById("tagUpdate").className = "tag ok";
  }

  function isTradingTime() {
    var d = new Date(), day = d.getDay();
    if (day === 0 || day === 6) return false;
    var hm = d.getHours() * 100 + d.getMinutes();
    return (hm >= 915 && hm <= 1505);
  }

  function loadAll() {
    if (state.loading) return;
    state.loading = true;
    document.getElementById("tagUpdate").textContent = "加载中…";
    document.getElementById("tagUpdate").className = "tag";
    try {
      var jobs = [trends(INDEX_SECID)].concat(ETFS.map(function (e) { return trends(e.m + "." + e.code); }));
      jobs.push(snapshot());
      /* 21 只标的 + 1 个快照并发拉取，任何一个超时都会让 Promise.all 整体失败，
         页面就一直停在"刷新失败，保留上次数据"。改成逐个容错：
         指数必须有（没有就没法画），ETF 允许个别缺失，但成功率不足 80% 时宁可不画 ——
         少几只的合计成交额会偏低，画出来是错的。 */
      var safe = function (p) {
        return p.then(function (v) { return { ok: true, v: v }; },
                      function () { return { ok: false }; });
      };
      Promise.all(jobs.map(safe)).then(function (rs) {
        var indexTr = rs[0].ok ? rs[0].v : null;
        var snap = rs[rs.length - 1].ok ? rs[rs.length - 1].v : null;
        var etfTrs = [];
        for (var k = 1; k < rs.length - 1; k++) if (rs[k].ok) etfTrs.push(rs[k].v);
        var need = Math.ceil(ETFS.length * 0.8);
        if (!indexTr || !indexTr.rows || !indexTr.rows.length) {
          document.getElementById("tagUpdate").textContent = "指数分时拉取失败，保留上次数据";
          document.getElementById("tagUpdate").className = "tag err";
          state.loading = false;
          return;
        }
        if (etfTrs.length < need) {
          document.getElementById("tagUpdate").textContent =
            "仅取到 " + etfTrs.length + "/" + ETFS.length + " 只ETF，保留上次数据";
          document.getElementById("tagUpdate").className = "tag err";
          state.loading = false;
          return;
        }
        renderLive(indexTr, etfTrs, snap);
        state.loading = false;
      }).catch(function (e) {
        document.getElementById("tagUpdate").textContent = "刷新失败，保留上次数据";
        document.getElementById("tagUpdate").className = "tag err";
        state.loading = false;
      });
    } catch (e) {
      document.getElementById("tagUpdate").textContent = "当前环境不支持实时拉取，展示内嵌数据";
      document.getElementById("tagUpdate").className = "tag err";
      state.loading = false;
    }
  }

  function tick() {
    if (state.paused || state.loading) return;
    state.remain -= 1;
    var tag = document.getElementById("tagCountdown");
    if (state.remain <= 0) {
      state.remain = isTradingTime() ? REFRESH_TRADING : REFRESH_IDLE;
      if (isTradingTime()) loadAll();
      return;
    }
    tag.style.display = isTradingTime() ? "" : "none";
    tag.textContent = "下次刷新 " + state.remain + "s";
  }

  on(document.getElementById("btnRefresh"), "click", function () {
    state.remain = isTradingTime() ? REFRESH_TRADING : REFRESH_IDLE;
    loadAll();
  });
  on(document.getElementById("btnPause"), "click", function () {
    state.paused = !state.paused;
    this.textContent = state.paused ? "恢复自动刷新" : "暂停自动刷新";
  });
  on(window, "resize", function () { if (chart) chart.resize(); if (cumChart) cumChart.resize(); });

  renderStatic();
  setTimeout(loadAll, 1500);
  setInterval(tick, 1000);
})();

(function () {
  "use strict";
  var D = (window.__ISLANDS && window.__ISLANDS.cmpData) || {};
  var support = D.support || [], today = D.today || {}, recent = D.recent || [], latest = D.latest || {};
  if (!support.length) return;
  function on(t, ev, fn) { if (t && typeof t.addEventListener === "function") t.addEventListener(ev, fn, false); }
  function fmt(v, n) { return Number(v).toFixed(n == null ? 1 : n); }

  document.getElementById("tagAmt").textContent = "数据截至 " + today.date + "：" + fmt(today.amount, 1) + "亿（正在获取实时…）";
  function prevTradingDay(offset) {
    var d = new Date();
    d.setHours(0, 0, 0, 0);
    var n = 0;
    while (n < offset) { d.setDate(d.getDate() - 1); var w = d.getDay(); if (w !== 0 && w !== 6) n++; }
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
  }
  var expect = prevTradingDay(1);
  var stale = !!latest.date && latest.date < expect;
  var nfTag = document.getElementById("tagNf");
  nfTag.textContent = "最新净流入（" + latest.date + "，T-1）：" + fmt(latest.netflow, 1) +
    "亿 ｜ 成交额 " + fmt(latest.amount, 1) + "亿" +
    (stale ? " ｜ 净流入待更新（应为 " + expect + "：上游 island 已停更，本地份额库未覆盖深市 ETF）" : "");
  if (stale) nfTag.className = "tag warn";

  var c1 = null, c2 = null;
  /* 近期交易日（黄柱）优先级高于托底日配色：近期日里若有一天本身净流入就 > 100 亿，
     它同时也躺在 support 里。此前用 cats.indexOf(date) < 0 去重 —— 结果是该日永远只画成
     红/蓝柱，黄柱被吞掉，用户就「看不到那一天」。现在改为命中即改色，且整体按日期排序。 */
  function buildSeries(metric, extraBars, yName) {
    /* 成交额=蓝、净流入=红 —— 与页面其余部分统一：上方量能类图主色为蓝，
       下方资金/净流入类图主题多为红（红=净申购、绿=净赎回）。 */
    var barColor = metric === "netflow" ? "#d93630" : "#4c7bf3";
    var hl = {}, order = [];
    (extraBars || []).forEach(function (r) {
      if (r && r.date) { hl[r.date] = r[metric]; order.push(r.date); }
    });
    var rows = [], seen = {};
    support.forEach(function (s) {
      seen[s.date] = 1;
      var isHl = Object.prototype.hasOwnProperty.call(hl, s.date);
      rows.push({ date: s.date, value: isHl ? hl[s.date] : s[metric],
                  color: isHl ? "#f0b429" : barColor });
    });
    order.forEach(function (d) {                  // support 里没有的近期日，追加
      if (!seen[d]) { seen[d] = 1; rows.push({ date: d, value: hl[d], color: "#f0b429" }); }
    });
    rows.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
    return {
      cats: rows.map(function (r) { return r.date; }),
      vals: rows.map(function (r) {
        return { value: r.value, itemStyle: { color: r.color } };
      })
    };
  }

  /* 「最新值」标签：钉在图内右上角、绘图区之上（标题行右侧）。
     此前放在基准线附近：正值柱从 0 向上、负值柱从 0 向下，紧邻线上方必然压住自己的柱子，
     抬高又会撞到左右邻居；改到这里后任何取值都不会遮挡柱子，且仍在图表内部。 */
  function latestValueTag(v) {
    var txt = "最新值 " + fmt(v, 1) + "亿";
    var w = 0;
    for (var i = 0; i < txt.length; i++) w += (txt.charCodeAt(i) > 255 ? 12 : 6.6);
    w = Math.ceil(w) + 20;
    return {
      type: "group", right: 14, top: 30, z: 120,
      children: [
        { type: "rect", shape: { width: w, height: 24, r: 4 },
          style: { fill: "rgba(255,255,255,0.96)", stroke: "#e0c48a", lineWidth: 1,
                   shadowBlur: 4, shadowColor: "rgba(0,0,0,0.14)" } },
        { type: "text", style: { text: txt, x: 10, y: 12, fill: "#8a5a00",
                                 font: "500 12px Arial,sans-serif",
                                 align: "left", verticalAlign: "middle" } }
      ]
    };
  }

  function drawSupportChart(containerId, metric, extraBars, title, yName, liveLabel) {
    var data = buildSeries(metric, extraBars, yName);
    /* 本图专属的「待公布」日期：净流入最新一日还没出数，在该位置铺一块灰底 + 标注，
       占住与上图第 3 根黄柱相同的横向位置，避免上图 3 根、下图 2 根的错位观感。
       必须是本图局部变量——若用模块级变量，成交额图 hover 时会读到净流入图的日期。 */
    var pendDate = null;
    (extraBars || []).forEach(function (r) { if (r && r.pending) pendDate = r.date; });
    var pendArea = pendDate ? {
      silent: true,
      itemStyle: { color: "rgba(138,147,163,0.16)" },
      label: { show: true, position: "insideTop", distance: 5, formatter: "待公布",
               color: "#8a93a3", fontSize: 11 },
      data: [[{ xAxis: pendDate }, { xAxis: pendDate }]]
    } : undefined;
    /* 取「最后一个有值」的日子，而不是最后一项：净流入图的横轴末位是最新交易日
       的空位（尚未公布、无柱子），虚线与「最新值」仍应指向昨天那根真实柱子。 */
    var lastVal = null;
    for (var li = extraBars.length - 1; li >= 0; li--) {
      if (extraBars[li] && extraBars[li][metric] != null) { lastVal = extraBars[li][metric]; break; }
    }
    /* label.show:false —— markLine 默认会在直线末端（贴右边）渲染 yAxis 数值，
       和右上角「最新值」浮层重复，且看上去像没擦干净的残影。 */
    var ml = [], gfx = [];
    if (lastVal != null) {
      ml.push({ yAxis: lastVal, lineStyle: { type: "dashed", color: "#b07a1a", width: 1.5 },
                label: { show: false } });
      gfx.push(latestValueTag(lastVal));
    }
    var today = new Date();
    var tstr = today.getFullYear() + "-" + ("0" + (today.getMonth() + 1)).slice(-2) + "-" + ("0" + today.getDate()).slice(-2);
    var chart = echarts.init(document.getElementById(containerId));
    chart.setOption({
      title: { text: title, left: 8, top: 6, padding: 0, textStyle: { fontSize: 14 } },
      grid: { top: 56, left: 16, right: 8, bottom: 48, containLabel: true },
      tooltip: { trigger: "axis", formatter: function (ps) {
        var x = ps[0];
        var tag = (extraBars || []).some(function (r) { return r.date === x.axisValue; }) ? "（近期）" : "";
        var v = (pendDate && x.axisValue === pendDate) ? "待公布"
              : (x.value == null ? "--" : fmt(x.value, 1) + " 亿");
        return [x.axisValue + tag, yName + "：" + v].join("<br/>");
      } },
      xAxis: { type: "category", data: data.cats, axisLabel: { hideOverlap: true, rotate: 40 } },
      yAxis: { type: "value", name: "亿元", scale: true, nameTextStyle: { align: "left" } },
      dataZoom: [ { type: "inside", xAxisIndex: 0 }, { type: "slider", xAxisIndex: 0, height: 14, bottom: 12 } ],
      series: [ { type: "bar", data: data.vals,
                  markLine: { symbol: "none", label: { show: false }, data: ml },
                  markArea: pendArea } ],
      graphic: gfx
    }, true);
    return chart;
  }

  /* 从 dailyData 取最近 N 个「去重」交易日作为境外黄柱 —— dailyData 是唯一真源，每日由
     tools/fetch_netflow.py 追加。早前用 cmpData.recent + cmpData.today 拼接：dailyData 一旦
     已含当日，两者同日期会被 buildSeries 去重，标题写「最近3个交易日」却只画出 2 根。 */
  var dailyD = (window.__ISLANDS && window.__ISLANDS.dailyData) || {};
  var TRAIL_AMT = 3, TRAIL_NF = 2;

  function trailingRows(n, metric) {
    var ds = dailyD.dates || [], amt = dailyD.amount || [], nf = dailyD.netflow || [];
    if (!ds.length) {                                  // 兜底：退回旧的 recent + today 拼接
      return recent.slice(-(n - 1)).concat([today]).filter(function (r) { return r && r.date; });
    }
    var out = [], seen = {};
    for (var i = ds.length - 1; i >= 0 && out.length < n; i--) {
      if (!ds[i] || seen[ds[i]]) continue;
      /* 净流入是 T-1 口径：dailyData 末日虽有成交额但净流入尚未公布（null），
         必须跳过，否则图上会多出一根没有意义的黄柱。 */
      if (metric && (metric === "netflow" ? nf[i] : amt[i]) == null) continue;
      seen[ds[i]] = 1;
      out.push({ date: ds[i], amount: amt[i], netflow: nf[i] });
    }
    return out.reverse();
  }

  function withLiveToday(rows) {
    /* 没有有效实时值（开盘前/停牌，成交额为 0 或空）时，原样返回历史最近 N 天，
       不要把 0 覆盖到最后一根历史柱子上（那会显示成「最新值 0.0亿」）。 */
    if (!today.date || !(today.amount > 0)) return rows.slice(-TRAIL_AMT);
    var hit = false;
    rows.forEach(function (r) { if (r.date === today.date) { r.amount = today.amount; hit = true; } });
    if (!hit) rows.push({ date: today.date, amount: today.amount });
    return rows.slice(-TRAIL_AMT);
  }

  /* 净流入图的近期日期 **直接沿用成交额图那一组**：有值就画黄柱，尚未公布（T-1 口径）
     就画灰色「待公布」占位（见 drawSupportChart 的 markArea）。这样两图横轴的近期格子
     数永远一致——此前各取各的（上图取 3 个交易日、下图取 2 个已公布日 + 1 个占位），
     一旦 today.date 没推进（开盘前、周末），占位条件不成立，就会退化成"上图 3 根、下图 2 根"。 */
  function nfRowsFor(amtRows) {
    var ds = dailyD.dates || [], nf = dailyD.netflow || [], map = {};
    ds.forEach(function (d, i) { map[d] = nf[i]; });
    return amtRows.map(function (r) {
      var v = map[r.date];
      return (v == null) ? { date: r.date, netflow: null, amount: null, pending: true }
                         : { date: r.date, netflow: v };
    });
  }

  var supportN = support.length + "个托底日";
  function amtTitle() {
    return "日度成交额：" + supportN + "（蓝） + 最近" + TRAIL_AMT + "个交易日（黄）· 时间序";
  }
  function nfTitle() {
    return "日度净流入：" + supportN + "（红） + 最近" + TRAIL_AMT + "个交易日（黄）· 时间序";
  }

  /* 首屏先按 dailyData 画（历史口径），拿到实时行情日期后再由 refreshToday() 重画，
     避免把今天的实时值写到昨天的柱子上。两图共用同一组近期日期，保证横轴对齐。 */
  var amtBase = trailingRows(TRAIL_AMT);
  c1 = drawSupportChart("chartAmt", "amount", amtBase, amtTitle(), "当日成交额");
  c2 = drawSupportChart("chartNf", "netflow", nfRowsFor(amtBase), nfTitle(), "当日净流入");

  /* live today amount refresh */
  var SECIDS = "1.510310,1.588000,0.159915,1.510300,1.563360,0.159352,1.512100,0.159338,1.510500,0.159949,1.588080,1.510050,1.510210,0.159845,0.159952,0.159361,1.512050,0.159633,1.560010,0.159919";
  function jsonp2(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var cb = "__scb" + Date.now() + "_" + Math.floor(Math.random() * 1e6);
      var s = document.createElement("script");
      var timer = setTimeout(function () { cleanup(); reject(new Error("timeout")); }, timeoutMs || 10000);
      function cleanup() { clearTimeout(timer); try { delete window[cb]; } catch (e) { window[cb] = undefined; } if (s.parentNode) s.parentNode.removeChild(s); }
      window[cb] = function (data) { cleanup(); resolve(data); };
      s.onerror = function () { cleanup(); reject(new Error("network error")); };
      s.src = url + "&cb=" + cb;
      (document.head || document.body).appendChild(s);
    });
  }
  function isTradingTime() {
    var d = new Date(), day = d.getDay();
    if (day === 0 || day === 6) return false;
    var hm = d.getHours() * 100 + d.getMinutes();
    return (hm >= 915 && hm <= 1505);
  }
  /* 当天是「哪一交易日」以行情为准，不能用 data.js 里存的日期：
     存的是上一次构建时的日期，跨日不刷新会把今天的实时值写到昨天的柱子上。 */
  function marketDate() {
    return fetch("/api/kline?symbol=sh000001&scale=1&datalen=1")
      .then(function (r) { return r.json(); })
      .then(function (arr) {
        return (arr && arr[0] && arr[0].day) ? String(arr[0].day).slice(0, 10) : null;
      })
      .catch(function () { return null; });
  }

  function nowStr() {
    var t = new Date();
    return t.getFullYear() + "-" + ("0" + (t.getMonth() + 1)).slice(-2) + "-" +
           ("0" + t.getDate()).slice(-2);
  }

  function refreshToday() {
    return marketDate().then(function (d) {
      /* 行情返回的日期只有确实是「今天」才算今天。开盘前（9:30 之前）与周末，
         接口最新一根还是上一交易日，此时 today.date 应停在那个交易日上，
         绝不能把当天 0 成交额写进那根历史柱子里。 */
      var isToday = d && d === nowStr();
      if (isToday && d !== today.date) today.date = d;
      var u = "/api/hq?codes=" + SECIDS.split(",").map(function (s) {
        var p = String(s).split(".");
        return (p[0] === "1" ? "sh" : "sz") + p[1];
      }).join(",");
      return fetch(u).then(function (r) { return r.json(); }).then(function (r) {
        var diff = (r && r.data && r.data.diff) || [];
        if (!diff.length) return;
        var sum = 0;
        diff.forEach(function (x) { sum += Number(x.f6); });
        /* 开盘前快照成交额为 0：这不是「今天成交 0 亿」，而是根本没开盘。
           直接 return，保留上一交易日的收盘值与图表，别把 0 写进历史柱子。 */
        if (!(sum > 0)) {
          document.getElementById("tagAmt").textContent =
            "上一交易日（" + today.date + "）成交额：" + fmt(today.amount, 1) + "亿（未开盘）";
          return;
        }
        today.amount = +(sum / 1e8).toFixed(2);
        today.amountPct = +(support.filter(function (s) { return s.amount < today.amount; }).length
                            / support.length * 100).toFixed(1);
        document.getElementById("tagAmt").textContent =
          (isToday ? "今日成交额（" + today.date + "，实时）：" : "上一交易日（" + today.date + "）成交额：")
          + fmt(today.amount, 1) + "亿";
        /* 两图共用同一组近期日期：成交额图取「最近 N 个交易日（含今日实时）」，
           净流入图沿用同一组日期，缺数的画「待公布」灰底 —— 横轴永远对齐。 */
        var amtRows = withLiveToday(trailingRows(TRAIL_AMT));
        if (c1) c1.dispose();
        c1 = drawSupportChart("chartAmt", "amount", amtRows, amtTitle(), "当日成交额");
        if (c2) c2.dispose();
        c2 = drawSupportChart("chartNf", "netflow", nfRowsFor(amtRows), nfTitle(), "当日净流入");
      });
    }).catch(function (e) {});
  }
  refreshToday();
  setInterval(function () { if (isTradingTime()) refreshToday(); }, 60000);
  on(window, "resize", function () { c1.resize(); c2.resize(); });
})();

(function () {
  "use strict";
  var D = (window.__ISLANDS && window.__ISLANDS.dailyData) || {};
  var dates = D.dates || [], amount = D.amount || [], netflow = D.netflow || [], idx = D.idx || [];
  if (!dates.length) return;
  function on(t, ev, fn) { if (t && typeof t.addEventListener === "function") t.addEventListener(ev, fn, false); }
  function fmt(v, n) { return Number(v).toFixed(n == null ? 2 : n); }
  function sign(v, n) { return (v > 0 ? "+" : "") + fmt(v, n); }

  var cumArr = [], acc = 0;
  for (var i = 0; i < netflow.length; i++) { acc += netflow[i]; cumArr.push(+acc.toFixed(1)); }

  function nfData() {
    var out = [];
    for (var i = 0; i < netflow.length; i++) {
      out.push({ value: netflow[i], itemStyle: { color: netflow[i] >= 0 ? "#d93630" : "#0aa06e" } });
    }
    return out;
  }

  function tooltipFmt(mode) {
    return function (ps) {
      var i = ps[0].dataIndex, out = [ps[0].axisValue];
      ps.forEach(function (p) {
        if (p.seriesType === "bar") out.push((mode === "nf" ? "当日净流入：" : "当日成交额：") + sign(p.value, 2) + " 亿");
        else out.push("上证指数：" + fmt(p.value, 2));
      });
      if (mode === "nf" && cumArr[i] != null) out.push("累计净流入：" + sign(cumArr[i], 1) + " 亿");
      return out.join("<br/>");
    };
  }

  function baseOption(title, barName, barData, tipFn) {
    return {
      title: { text: title, left: 8, top: 6, padding: 0, textStyle: { fontSize: 14 } },
      legend: { data: [barName, "上证综指"], left: 8, top: 26, type: "scroll", height: 24, itemWidth: 16, itemHeight: 10 },
      grid: { top: 76, left: 16, right: 8, bottom: 56, containLabel: true },
      tooltip: { trigger: "axis", formatter: tipFn },
      xAxis: { type: "category", data: dates, axisLabel: { hideOverlap: true } },
      yAxis: [
        { type: "value", name: "亿元", scale: true, nameTextStyle: { align: "left" } },
        { type: "value", name: "上证指数（点）", scale: true, nameTextStyle: { align: "right" } }
      ],
      /* moveOnMouseMove:false —— 让左键拖动留给区间框选，而不是 dataZoom 平移 */
      dataZoom: [ { type: "inside", xAxisIndex: 0, moveOnMouseMove: false, zoomOnMouseWheel: true },
                  { type: "slider", xAxisIndex: 0, height: 16, bottom: 14 } ],
      series: [
        { name: barName, type: "bar", data: barData },
        { name: "上证综指", type: "line", yAxisIndex: 1, data: idx, connectNulls: true, showSymbol: false, lineStyle: { width: 1.6, color: "#e8862e" }, itemStyle: { color: "#e8862e" } }
      ]
    };
  }

  var charts = [];
  var c1 = echarts.init(document.getElementById("chartDAmt"));
  c1.setOption(baseOption("20只ETF日度成交额与上证综指", "20只ETF日度成交额合计", amount, tooltipFmt("amt")), true);
  var c2 = echarts.init(document.getElementById("chartDNf"));
  c2.setOption(baseOption("20只ETF日度净流入（申赎口径）与上证综指", "20只ETF日度净流入", nfData(), tooltipFmt("nf")), true);
  charts = [c1, c2];
  on(window, "resize", function () { charts.forEach(function (c) { if (c && c.resize) c.resize(); }); });
})();

(function () {
  "use strict";
  var D = (window.__ISLANDS && window.__ISLANDS.typData) || {};
  var slots = D.slots || [], supAmt = D.supAmt || [], norAmt = D.norAmt || [], supIdx = D.supIdx || [], norIdx = D.norIdx || [];
  if (!slots.length) return;
  function on(t, ev, fn) { if (t && typeof t.addEventListener === "function") t.addEventListener(ev, fn, false); }
  function fmt(v, n) { return Number(v).toFixed(n == null ? 1 : n); }

  function draw(containerId, title, barName, bars, idxPath, barColor) {
    var chart = echarts.init(document.getElementById(containerId));
    chart.setOption({
      title: { text: title, left: 8, top: 6, padding: 0, textStyle: { fontSize: 14 } },
      legend: { data: [barName, "上证综指"], left: 8, top: 26, type: "scroll", height: 24, itemWidth: 16, itemHeight: 10 },
      grid: { top: 76, left: 16, right: 8, bottom: 46, containLabel: true },
      tooltip: { trigger: "axis", formatter: function (ps) {
        var out = [ps[0].axisValue];
        ps.forEach(function (p) {
          if (p.seriesType === "bar") out.push(p.seriesName + "：" + fmt(p.value, 1) + " 亿");
          else out.push("上证综指：" + (p.value > 0 ? "+" : "") + fmt(p.value, 2) + "%（相对开盘）");
        });
        return out.join("<br/>");
      } },
      xAxis: { type: "category", data: slots, axisLabel: { hideOverlap: true } },
      yAxis: [
        { type: "value", name: "亿元/15分钟", scale: true, nameTextStyle: { align: "left" } },
        { type: "value", name: "上证综指（相对开盘%）", scale: true, nameTextStyle: { align: "right" } }
      ],
      series: [
        { name: barName, type: "bar", data: bars, itemStyle: { color: barColor } },
        { name: "上证综指", type: "line", yAxisIndex: 1, data: idxPath, showSymbol: false,
          lineStyle: { width: 1.6, color: "#e8862e" }, itemStyle: { color: "#e8862e" } }
      ]
    }, true);
    return chart;
  }

  var c1 = draw("chartTypCase", "放量案例日 · 20大ETF量能与上证综指（15分钟均值）", "放量案例日成交额均值", supAmt, supIdx, "#d93630");
  var c2 = draw("chartTypNorm", "常态交易日 · 20大ETF量能与上证综指（15分钟均值）", "常态日成交额均值", norAmt, norIdx, "#4c7bf3");
  on(window, "resize", function () { c1.resize(); c2.resize(); });
})();

/* ==========================================================================
   第三部分补充图（2026-10-15 换仓后新增）
   全市场宽基ETF日度净流入（按跟踪指数分类堆积）+ 累计净流入线（右轴）
   ========================================================================== */
(function () {
  "use strict";
  var I = (window.__ISLANDS) || {};
  var BR = I.broadData || {};
  function on(t, ev, fn) { if (t && typeof t.addEventListener === "function") t.addEventListener(ev, fn, false); }
  function fmt(v, n) { return Number(v).toFixed(n == null ? 1 : n); }
  function sign(v, n) { return (v > 0 ? "+" : "") + fmt(v, n); }
  function has(el) { return el && document.getElementById(el); }

  if ((BR.dates || []).length && has("chartBroad")) {
    var bseries = (BR.cats || []).map(function (c, i) {
      return { name: c, type: "bar", stack: "broad", data: BR.series[c],
               itemStyle: { color: BR.colors[i % BR.colors.length] } };
    });
    bseries.push({
      name: "累计净流入", type: "line", yAxisIndex: 1, data: BR.totalCum,
      showSymbol: false, lineStyle: { width: 2.2, color: "#1f2d3d" }, itemStyle: { color: "#1f2d3d" }
    });
    var ch = echarts.init(document.getElementById("chartBroad"));
    ch.setOption({
      title: { text: "2026年以来 全市场宽基ETF日度净流入与累计净流入", left: 8, top: 6, padding: 0, textStyle: { fontSize: 14 } },
      legend: { top: 26, left: 8, type: "scroll", height: 24, itemWidth: 12, itemHeight: 10 },
      grid: { top: 76, left: 16, right: 16, bottom: 56, containLabel: true },
      tooltip: {
        trigger: "axis",
        formatter: function (arr) {
          var tot = 0, out = [arr[0].axisValue];
          arr.forEach(function (p) {
            if (p.seriesType !== "bar") return;
            tot += (p.value || 0);
            if (p.value) out.push('<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
                                  p.color + ';margin-right:5px"></span>' + p.seriesName + "：" + sign(p.value, 1) + " 亿");
          });
          out.push("当日合计：" + sign(tot, 1) + " 亿");
          arr.forEach(function (p) {
            if (p.seriesType === "line" && p.value != null) out.push("累计净流入：" + sign(p.value, 0) + " 亿");
          });
          return out.join("<br/>");
        }
      },
      xAxis: { type: "category", data: BR.dates, axisLabel: { hideOverlap: true } },
      yAxis: [
        { type: "value", name: "当日净流入（亿元）", scale: true, nameTextStyle: { align: "left" } },
        { type: "value", name: "累计净流入（亿元）", scale: true, nameTextStyle: { align: "right" },
          splitLine: { show: false } }
      ],
      /* moveOnMouseMove:false —— 让左键拖动留给区间框选，而不是 dataZoom 平移 */
      dataZoom: [{ type: "inside", xAxisIndex: 0, moveOnMouseMove: false, zoomOnMouseWheel: true },
                 { type: "slider", xAxisIndex: 0, height: 16, bottom: 14 }],
      series: bseries
    }, true);
    window.__chBroad = ch;
  }

  on(window, "resize", function () { if (window.__chBroad) window.__chBroad.resize(); });

  /* ---------- 最近一个月 · 分类日度净流入表格（条件格式） ---------- */
  /* 颜色深浅按「当列」的最大绝对值归一化：各分类量级差很远（沪深300动辄几十亿、
     国证2000常常不到1亿），若按全局归一，小分类会永远是白色、等于白做条件格式。 */
  if (has("broadTable") && (BR.dates || []).length) {
    var RN = 22;                                       // 最近约一个月的交易日
    var rows = [];
    for (var ri = BR.dates.length - 1; ri >= 0 && rows.length < RN; ri--) rows.push(ri);
    var cols = ["合计"].concat(BR.cats || []);
    var colMax = cols.map(function (c) {
      var m = 0;
      rows.forEach(function (i) {
        var v = c === "合计" ? BR.total[i] : BR.series[c][i];
        if (v != null && Math.abs(v) > m) m = Math.abs(v);
      });
      return m || 1;
    });
    function cellStyle(v, k) {
      if (v == null || !v) return "";
      var t = Math.min(1, Math.abs(v) / colMax[k]);
      if (t < 0.02) return "";
      var a = (0.06 + 0.5 * t).toFixed(3);
      return " style='background:" + (v > 0 ? "rgba(217,54,48," : "rgba(10,160,110,") + a + ");'";
    }
    function cellTxt(v, dec) {
      if (v == null) return "--";
      return (v > 0 ? "+" : "") + Number(v).toFixed(dec);
    }
    var html = "<thead><tr><th>日期</th>" +
      cols.map(function (c) { return "<th class='num'>" + c + "</th>"; }).join("") + "</tr></thead><tbody>";
    rows.forEach(function (i) {
      html += "<tr><td>" + BR.dates[i] + "</td>";
      cols.forEach(function (c, k) {
        var v = c === "合计" ? BR.total[i] : BR.series[c][i];
        html += "<td class='num'" + cellStyle(v, k) + ">" +
                cellTxt(v, c === "合计" ? 1 : 2) + "</td>";
      });
      html += "</tr>";
    });
    html += "</tbody>";
    document.getElementById("broadTable").innerHTML = html;
  }
})();

/* ==========================================================================
   区间框选求和
   按住鼠标左键或右键，在图上从左向右拖出一段，松手后显示该区间的合计金额。
   应用于：分钟图(1) / 日度成交额(8) / 日度净流入(9) / 全市场宽基日度净流入(10)。

   两个实现要点：
   1) 求和只累加 type==="bar" 的系列，排除上证综指、累计净流入这些折线，
      否则会把点位/累计值加进金额里。堆积图（全市场宽基）各分类会自动加总成当日合计。
   2) 这几个图的 inside dataZoom 已改为 moveOnMouseMove:false，
      否则左键拖动会被 ECharts 当成平移缩放，框选永远拖不出来。
   ========================================================================== */
(function () {
  "use strict";
  if (typeof echarts === "undefined") return;

  function num(v) {
    if (v == null) return null;
    if (typeof v === "object" && "value" in v) v = v.value;
    return (typeof v === "number" && isFinite(v)) ? v : null;
  }

  function rangeBrush(containerId, label, unit) {
    var el = document.getElementById(containerId);
    if (!el || !el.parentNode) return;
    var wrap = el.parentNode;
    if (wrap.style.position !== "relative" && wrap.style.position !== "absolute") {
      wrap.style.position = "relative";
    }
    var band = document.createElement("div");
    band.className = "brush-band";
    var tip = document.createElement("div");
    tip.className = "brush-tip";
    wrap.appendChild(band);
    wrap.appendChild(tip);

    var dragging = false, x0 = 0, x1 = 0, SLIDER_H = 44;

    function rect() { return el.getBoundingClientRect(); }
    function localX(e) { return e.clientX - rect().left; }
    function localY(e) { return e.clientY - rect().top; }

    function hide() { band.style.display = "none"; tip.style.display = "none"; }

    function offsetInWrap() {
      var wr = wrap.getBoundingClientRect(), er = rect();
      return { ox: er.left - wr.left, oy: er.top - wr.top };
    }

    function drawBand(a, b) {
      var o = offsetInWrap();
      band.style.display = "block";
      band.style.left = (o.ox + Math.min(a, b)) + "px";
      band.style.top = o.oy + "px";
      band.style.width = Math.abs(b - a) + "px";
      band.style.height = el.clientHeight + "px";
    }

    function idxAt(x) {
      var inst = echarts.getInstanceByDom(el);
      if (!inst) return null;
      var o = inst.getOption();
      var cats = (o.xAxis && o.xAxis[0] && o.xAxis[0].data) || [];
      if (!cats.length) return null;
      /* 必须用 gridIndex 作 finder：实测 convertFromPixel({xAxisIndex:0}) 返回 null，
         拿不到索引就会静默失败（表现为拖得出来、但不显示合计）。 */
      var p = inst.convertFromPixel({ gridIndex: 0 }, [x, 0]);
      if (!p || !isFinite(p[0])) return null;
      return Math.max(0, Math.min(cats.length - 1, Math.round(p[0])));
    }

    function compute(a, b) {
      var inst = echarts.getInstanceByDom(el);
      if (!inst) return;
      var o = inst.getOption();
      var cats = (o.xAxis && o.xAxis[0] && o.xAxis[0].data) || [];
      var i0 = idxAt(Math.min(a, b)), i1 = idxAt(Math.max(a, b));
      if (i0 == null || i1 == null || !cats.length) return;
      var sum = 0, hit = 0;
      (o.series || []).forEach(function (s) {
        if (s.type !== "bar") return;          // 只要柱状：排除折线（指数点位、累计净流入）
        var d = s.data || [];
        for (var i = i0; i <= i1; i++) {
          var v = num(d[i]);
          if (v != null) { sum += v; hit++; }
        }
      });
      var span = i1 - i0 + 1;
      var txt = label + "合计 " + sum.toFixed(1) + " 亿元";
      /* 单位按图区分：分钟图写「分钟」，日度图写「交易日」，不写笼统的「项」 */
      if (span > 1) txt += "｜" + cats[i0] + " ~ " + cats[i1] + "（" + span + " " + (unit || "项") + "）";
      else txt += "｜" + cats[i0];

      var o2 = offsetInWrap();
      tip.textContent = txt;
      tip.style.display = "block";
      tip.style.left = (o2.ox + (a + b) / 2) + "px";
      tip.style.top = (o2.oy + 4) + "px";
    }

    el.addEventListener("mousedown", function (e) {
      if (e.button !== 0 && e.button !== 2) return;    // 只认左键 / 右键
      if (localY(e) > el.clientHeight - SLIDER_H) return;  // 底部是 dataZoom 滑块，别抢
      dragging = true;
      x0 = x1 = localX(e);
      drawBand(x0, x1);
      tip.style.display = "none";
      e.preventDefault();
    });
    el.addEventListener("contextmenu", function (e) { e.preventDefault(); });

    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      x1 = localX(e);
      drawBand(x0, x1);
    });
    window.addEventListener("mouseup", function (e) {
      if (!dragging) return;
      dragging = false;
      if (Math.abs(x1 - x0) < 4) { hide(); return; }   // 视作单击，清除
      compute(x0, x1);
    });
  }

  /* 图表实例在前面各段里同步创建，这里直接绑定即可 */
  rangeBrush("chart", "成交额", "分钟");
  rangeBrush("chartDAmt", "成交额", "交易日");
  rangeBrush("chartDNf", "净流入", "交易日");
  rangeBrush("chartBroad", "净流入", "交易日");
})();
