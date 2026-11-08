# -*- coding: utf-8 -*-
"""把当前一帧数据 + 全部静态资源打成单个 HTML，用于 GitHub Pages / OSS 静态托管。

用法：
    python scripts/make_snapshot.py [输出路径]      # 默认 /tmp/etf_flow_snapshot.html

原理：在页面里注入 window.__ETF_SNAPSHOT（board + bundle 各一帧），并在
window.fetch 层把 /v1/etf_flow/board|bundle 截胡成读内联数据；装载器与渲染
脚本一字不改，所以快照和在线版渲染结果完全一致。

注意：这是某一时刻的静态帧，不会自动刷新；需要实时行情请运行 app.py。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read_static(rel: str) -> str:
    with open(os.path.join(ROOT, rel.lstrip("/")), encoding="utf-8") as f:
        return f.read()


def payload(client, path):
    body = client.get(path).get_json()
    if not body or body.get("code") != 20000:
        raise RuntimeError("%s 返回失败: %s" % (path, (body or {}).get("message")))
    return body["data"]


def main(out: str):
    import app as appmod  # noqa: E402  （需要先插入 ROOT 到 sys.path）

    client = appmod.app.test_client()
    board = payload(client, "/v1/etf_flow/board")
    bundle = payload(client, "/v1/etf_flow/bundle")
    built_at = bundle.get("built_at") or time.strftime("%Y-%m-%d %H:%M:%S")

    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()

    # 1) CSS / 直接引用的 script 全部内联
    html = re.sub(
        r'<link rel="stylesheet" href="(/static/[^"]+)">',
        lambda m: "<style>\n%s\n</style>" % read_static(m.group(1)),
        html,
    )
    html = re.sub(
        r'<script src="(/static/[^"]+)"></script>',
        lambda m: "<script>\n%s\n</script>" % read_static(m.group(1)),
        html,
    )

    # 2) 装载器：把两个动态注入的脚本源改成 Blob URL（单文件里没有 /static 路径）
    boot = read_static("/static/js/etf_flow_board_boot.js")
    boot = re.sub(r"var ENHANCE_SRC = .*?;\n",
                  'var ENHANCE_SRC = __blob("src-enhance");\n', boot, count=1)
    boot = re.sub(r"var BOARD_SRC = .*?;\n",
                  'var BOARD_SRC = __blob("src-board");\n', boot, count=1)

    enhance = read_static("/static/js/etf_flow_board_enhance.js").replace("</script", "<\\/script")
    board_js = read_static("/static/js/etf_flow_board.js").replace("</script", "<\\/script")

    snapshot = json.dumps({"board": board, "bundle": bundle}, ensure_ascii=False,
                          separators=(",", ":")).replace("</", "<\\/")

    inject = """<script>
window.__ETF_SNAPSHOT = %s;
window.__blob = function (id) {
  var el = document.getElementById(id);
  return URL.createObjectURL(new Blob([el.textContent], {type: 'application/javascript'}));
};
(function () {
  var real = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var key = url.indexOf('/v1/etf_flow/board') === 0 ? 'board'
            : url.indexOf('/v1/etf_flow/bundle') === 0 ? 'bundle' : null;
    if (key) {
      return Promise.resolve({ok: true, status: 200, json: function () {
        return Promise.resolve({code: 20000, message: 'success', data: window.__ETF_SNAPSHOT[key]});
      }});
    }
    return real ? real(input, init) : Promise.reject(new Error('fetch unavailable'));
  };
})();
</script>
<script id="src-enhance" type="text/plain">%s</script>
<script id="src-board" type="text/plain">%s</script>
<script>
%s
</script>""" % (snapshot, enhance, board_js, boot)

    html, n = re.subn(r'<script id="efBoardBoot"[\s\S]*?</script>', lambda _: inject, html)
    if n != 1:
        raise RuntimeError("未在模板中找到 efBoardBoot 标签，模板结构可能已变化")

    banner = ('<div style="max-width:1600px;margin:0 auto;padding:8px 20px;'
              'background:#fff4e0;color:#9a5b00;font-size:12px">'
              '静态快照 · 行情时间 %s · 数据不会自动刷新，实时版请运行 app.py</div>' % built_at)
    html = html.replace('<body class="etf-momentum-page">',
                        '<body class="etf-momentum-page">\n' + banner, 1)

    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("快照已生成: %s (%.1f MB, 行情时间 %s, %d 个标的)" % (
        out, os.path.getsize(out) / 1048576, built_at, len(bundle.get("kline") or {})))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/etf_flow_snapshot.html")
