/* 优化层：懒渲染 + 骨架屏收尾 + 主题收敛，逐字沿用独立复刻版 enhance.js。 */
/* 优化层：不改动 app.js 的分析逻辑，只在外围补四件事
   1) 统一主题 —— 收敛 dataZoom 滑块的默认白/紫配色，字体与动效统一
   2) 懒初始化 —— 7 张图分布在很长的一页里，首屏只画看得见的那一两张
   3) 骨架屏收尾 —— 首帧不至于白屏，脚本就绪后淡出
   4) 减少动态效果 —— 尊重 prefers-reduced-motion

   做法是包一层 echarts.init：容器不在视口附近时返回一个「排队桩」，
   把 setOption / resize 记下来，等 IntersectionObserver 触发再真正建实例并回放。
   app.js 完全不知道自己拿到的是桩还是真实例。 */
(function () {
  "use strict";
  if (!window.echarts) return;

  var realInit = echarts.init.bind(echarts);
  var reduced = false;
  try { reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}

  /* ---------- 1) 主题 ---------- */
  echarts.registerTheme("etf-lite", {
    color: ["#2b5fd9", "#d93630", "#e8862e", "#0aa06e", "#8a6fd1", "#c9a227"],
    textStyle: {
      fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif'
    },
    animationDuration: reduced ? 0 : 420,
    animationEasing: "cubicOut",
    dataZoom: {
      borderColor: "transparent",
      backgroundColor: "rgba(240, 243, 248, 0.7)",
      fillerColor: "rgba(43, 95, 217, 0.10)",
      handleSize: "90%",
      handleStyle: { color: "#ffffff", borderColor: "#b9c3d4", shadowBlur: 0 },
      moveHandleStyle: { color: "#c3cddd", opacity: 0.9 },
      dataBackground: {
        lineStyle: { color: "#cfd8e6", width: 1, opacity: 0.9 },
        areaStyle: { color: "rgba(207, 216, 230, 0.35)" }
      },
      selectedDataBackground: {
        lineStyle: { color: "#9fb2d6" },
        areaStyle: { color: "rgba(159, 178, 214, 0.35)" }
      },
      emphasis: { handleStyle: { borderColor: "#2b5fd9" } },
      textStyle: { color: "#8a94a6", fontSize: 10 }
    }
  });

  /* ---------- 2) 懒初始化 ---------- */
  var realByDom = new Map();   // dom → 真实例
  var stubByDom = new Map();   // dom → 排队桩
  var hasIO = typeof window.IntersectionObserver === "function";

  function offscreen(el) {
    var r = el.getBoundingClientRect();
    if (!r.width || !r.height) return true;          // display:none / 尺寸未定
    var vh = window.innerHeight || 0;
    var margin = 500;                                 // 提前一屏开始画，滚动时不至于空白
    return r.bottom < -margin || r.top > vh + margin;
  }

  function makeStub(el) {
    var stub = {
      __lazy: true,
      __dom: el,
      __option: null,
      __notMerge: false,
      setOption: function (option, notMerge) {
        stub.__option = option;
        stub.__notMerge = !!notMerge;
        return stub;
      },
      resize: function () { return stub; },
      dispose: function () { stubByDom.delete(el); if (io) io.unobserve(el); return undefined; },
      getOption: function () { return null; },
      getDom: function () { return el; },
      getWidth: function () { return el.clientWidth || 0; },
      getHeight: function () { return el.clientHeight || 0; },
      showLoading: function () {}, hideLoading: function () {},
      clear: function () { stub.__option = null; },
      on: function () { return stub; }, off: function () { return stub; },
      dispatchAction: function () {},
      isDisposed: function () { return !stubByDom.has(el); }
    };
    return stub;
  }

  function materialise(el) {
    var stub = stubByDom.get(el);
    stubByDom.delete(el);
    var inst = realInit(el, "etf-lite");
    realByDom.set(el, inst);
    if (stub && stub.__option) inst.setOption(stub.__option, stub.__notMerge);
    dropBoot();
    return inst;
  }

  var io = hasIO ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      if (stubByDom.has(en.target)) materialise(en.target);
    });
  }, { rootMargin: "500px 0px", threshold: 0 }) : null;

  echarts.init = function (el, theme, opts) {
    if (!el) return realInit(el, theme, opts);
    var existing = realByDom.get(el);
    if (existing && existing.isDisposed && existing.isDisposed()) {
      realByDom.delete(el);
      existing = null;
    }
    if (existing) return existing;
    if (!hasIO || !offscreen(el)) {
      var inst = realInit(el, "etf-lite", opts);
      realByDom.set(el, inst);
      dropBoot();
      return inst;
    }
    var stub = stubByDom.get(el);
    if (!stub) { stub = makeStub(el); stubByDom.set(el, stub); io.observe(el); }
    return stub;
  };

  /* 换窗宽/横竖屏时，桩按真实尺寸重绘；真实例照常 resize */
  window.addEventListener("orientationchange", function () {
    stubByDom.forEach(function (stub, el) {
      if (!offscreen(el)) materialise(el);
    });
  }, { passive: true });

  /* ---------- 3) 骨架屏 ---------- */
  var bootDropped = false;
  function dropBoot() {
    if (bootDropped) return;
    bootDropped = true;
    var boot = document.getElementById("boot");
    if (!boot) return;
    boot.classList.add("gone");
    setTimeout(function () { if (boot.parentNode) boot.parentNode.removeChild(boot); }, 500);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      setTimeout(dropBoot, 900);                 // 兜底：脚本异常也不会一直挡着
    });
  } else {
    setTimeout(dropBoot, 900);
  }
  window.addEventListener("load", function () { setTimeout(dropBoot, 200); });

  /* ---------- 4) 暂停按钮的 aria 状态 ---------- */
  var btn = document.getElementById("btnPause");
  if (btn) {
    btn.addEventListener("click", function () {
      var paused = /恢复/.test(btn.textContent || "");
      btn.setAttribute("aria-pressed", paused ? "true" : "false");
    });
  }

  /* ---------- 5) 数据构建时间：让人一眼看出这份快照有多新 ---------- */
  var stamp = document.getElementById("dataStamp");
  if (stamp && window.__ISLANDS) {
    var built = (window.__ISLANDS.cumData && window.__ISLANDS.cumData.builtAt) ||
                (window.__ISLANDS.initData && window.__ISLANDS.initData.date) || "";
    if (built) stamp.textContent = "数据快照 " + built;
  }

  /* ---------- 6) ?perf=1 自测标尺（默认不启用，不影响页面） ---------- */
  if (location.search.indexOf("perf=1") >= 0) {
    window.addEventListener("load", function () {
      var nav = (performance.getEntriesByType("navigation")[0]) || {};
      var res = performance.getEntriesByType("resource") || [];
      var bytes = res.reduce(function (a, r) { return a + (r.transferSize || 0); }, 0);
      var metrics = {
        dcl: Math.round(nav.domContentLoadedEventEnd || 0),
        load: Math.round(nav.loadEventEnd || 0),
        requests: res.length,
        transferKB: Math.round(bytes / 1024),
        chartsLive: realByDom.size,
        chartsLazy: stubByDom.size,
        viewport: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        widest: (function () {
          var worst = null, max = 0;
          document.querySelectorAll("body *").forEach(function (el) {
            var r = el.getBoundingClientRect();
            if (r.right > max && r.width > 0) { max = r.right; worst = el.tagName + "." + (el.className || "") + "#" + (el.id || ""); }
          });
          return { tag: worst, right: Math.round(max) };
        })()
      };
      var m = document.createElement("meta");
      m.name = "perf";
      m.content = JSON.stringify(metrics);
      document.head.appendChild(m);
    });
  }
})();
