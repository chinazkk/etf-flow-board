/* /etf_flow 装载器：把站点接口变成原版看板认得的 window.__ISLANDS，再依次拉起渲染脚本。

   两条接口：
     /v1/etf_flow/board   原版 data.js 的 island 全量（结构 1:1）
     /v1/etf_flow/bundle  21 个标的分钟线 + 实时快照，一次打包

   渲染脚本每轮会发 22 个 /api/kline、/api/hq 请求（21 个标的 + 1 个快照）。
   这里把这两条路径全部收口到同一个 bundle：客户端只看到 1 个请求，
   既不会撞站点 25/min 的爬虫限流，也省掉浏览器的连接排队。
   渲染脚本本身一字未改。 */
(function () {
  "use strict";

  var script = document.currentScript;
  var BOARD_API = "/v1/etf_flow/board";
  var BUNDLE_API = "/v1/etf_flow/bundle";
  var BUNDLE_TTL = 25000;          // 一屏之内多个图表共用同一份，别重复拉
  var ENHANCE_SRC = (script && script.dataset.enhance) || "/static/js/etf_flow_board_enhance.js";
  var BOARD_SRC = (script && script.dataset.board) || "/static/js/etf_flow_board.js";

  var realFetch = window.fetch ? window.fetch.bind(window) : null;

  function jsonFetch(url) {
    if (!realFetch) return Promise.reject(new Error("浏览器不支持 fetch"));
    return realFetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error(url + " → HTTP " + r.status);
      return r.json();
    }).then(function (body) {
      // 站点统一包一层 {code, message, data}，code 非 20000 视为业务失败
      if (body && typeof body.code !== "undefined" && body.code !== 20000) {
        throw new Error(body.message || "接口返回失败");
      }
      return body && Object.prototype.hasOwnProperty.call(body, "data") ? body.data : body;
    });
  }

  /* 渲染脚本拿到的必须是「像 Response 的东西」—— 它只用到 .json() */
  function fakeJson(value) {
    return { ok: true, status: 200, json: function () { return Promise.resolve(value); } };
  }

  var bundle = null, bundleAt = 0, bundlePending = null;

  /* 上游三路（东财 / 腾讯 / 新浪）都拿不到时才回吐上一帧（stale=true）。
     页面照样能画，但必须让看的人知道这是几点的数据，而不是图卡住了。 */
  var SOURCE_CN = { eastmoney: "东财", tencent: "腾讯", sina: "新浪", board: "看板" };

  function noteStale(data) {
    var box = document.getElementById("efLoadError");
    if (!box) return;
    if (data && data.stale) {
      var health = (data && data.sources_health) || {};
      var cooling = Object.keys(health).filter(function (k) {
        return health[k] && health[k].cooling;
      }).map(function (k) { return SOURCE_CN[k] || k; });
      box.hidden = false;
      box.className = "ef-error ef-warn";
      box.textContent = "行情源暂时不可用" +
        (cooling.length ? "（" + cooling.join("、") + " 已暂停重试）" : "") +
        "，当前显示 " + (data.built_at || "上一帧") + " 的数据，系统每分钟自动重试。";
    } else if (box.className.indexOf("ef-warn") >= 0) {
      box.hidden = true;
      box.className = "ef-error";
      box.textContent = "";
    }
  }

  function loadBundle() {
    if (bundle && Date.now() - bundleAt < BUNDLE_TTL) return Promise.resolve(bundle);
    if (bundlePending) return bundlePending;      // 22 个并发请求共用一次拉取
    bundlePending = jsonFetch(BUNDLE_API + "?t=" + Date.now()).then(function (data) {
      bundle = data || {};
      bundleAt = Date.now();
      bundlePending = null;
      noteStale(bundle);
      return bundle;
    }, function (err) {
      bundlePending = null;
      throw err;
    });
    return bundlePending;
  }

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";

    if (url.indexOf("/api/kline") === 0) {
      var m = /[?&]symbol=(sh|sz)(\d{6})/.exec(url);
      if (m) {
        return loadBundle().then(function (b) {
          var bars = (b.kline || {})[m[1] + m[2]];
          if (!bars || !bars.length) throw new Error("分时缺失: " + m[1] + m[2]);
          return fakeJson(bars);
        });
      }
    }

    if (url.indexOf("/api/hq") === 0) {
      var codes = (url.split("codes=")[1] || "").split(",");
      return loadBundle().then(function (b) {
        var diff = ((b.hq || {}).data || {}).diff || [];
        var want = {};
        codes.forEach(function (c) { if (c) want[String(c).trim().slice(-6)] = 1; });
        return fakeJson({
          rc: 0,
          data: { diff: diff.filter(function (x) { return x && want[x.f12]; }) }
        });
      });
    }

    return realFetch ? realFetch(input, init) : Promise.reject(new Error("浏览器不支持 fetch"));
  };

  function injectScript(src) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = src;
      el.async = false;                 // 保序：enhance 要先于 board
      el.onload = function () { resolve(); };
      el.onerror = function () { reject(new Error("脚本加载失败: " + src)); };
      document.head.appendChild(el);
    });
  }

  function dropBoot() {
    var boot = document.getElementById("boot");
    if (boot && boot.parentNode) boot.parentNode.removeChild(boot);
  }

  function fail(err) {
    dropBoot();
    var box = document.getElementById("efLoadError");
    if (box) {
      box.hidden = false;
      box.textContent = "看板数据加载失败：" + (err && err.message ? err.message : err) + "。稍后刷新，或检查服务端 /v1/etf_flow/board 是否可达。";
    }
  }

  function boot() {
    jsonFetch(BOARD_API).then(function (islands) {
      window.__ISLANDS = islands || {};
      return injectScript(ENHANCE_SRC);
    }).then(function () {
      return injectScript(BOARD_SRC);
    }).catch(fail);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
