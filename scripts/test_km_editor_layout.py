# -*- coding: utf-8 -*-
"""回归：「车站/线路」编辑里程的版面（2026-11-10 用户口径「都在下面太挤了」）

背景：线路详情独占侧栏，但上方常驻确认清单最多 360px、下面站序列表上千 px，
编辑区（工具条 + 段距列表 + 总里程 + 两大段说明，约 550px）被顶到 y=899
（窗口高 900）——整块在首屏之外，详情可视区只剩 291px 却要滚 2108px。

守这些事：
  - 编辑态由 `#paneNet.km-editing` 统一收起清单成表头、隐藏重复站序，
    **退出编辑移除 class 即自动还原**；NET.cfOpen / 用户折叠状态都不改。
  - 编辑区 DOM 紧跟 `#lnHead`（不能又排回「线路资料 / 真实走向」后面）。
  - 工具条 sticky、总里程行在段距列表**上面**、长说明默认折叠但可展开。
  - 段距 / 累计编辑的所有既有 id、保存 / 恢复 / 合计链路一个都不能少。
  - 只动版面，不动任何里程算法与后端接口。

跑法：python scripts/test_km_editor_layout.py
纯静态检查 + 可选 HTTP 冒烟（8686 没起就跳过，不误报）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK = FAIL = 0


def ck(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main():
    js = read("webapp/static/app.js")
    css = read("webapp/static/style.css")
    html = read("webapp/static/index.html")

    print("[1] 编辑态开关：lnWide / lnRenderEditor 统一 toggle，退出自动还原")
    ck("km-editing" in js, "app.js 里有 km-editing 开关")
    wide = js[js.find("function lnWide("):js.find("function lnWide(") + 900]
    ck("pane.classList.toggle('km-editing', !!on)" in wide,
       "lnWide() 里按开关 toggle 编辑态（所有入口统一）")
    ck("det.scrollTop = 0" in wide, "进入编辑时把详情滚回顶部")
    ck("classList.toggle('wide', !!on)" in wide, "侧栏放宽 390→560 仍在")
    ed = js[js.find("function lnRenderEditor("):js.find("function lnRenderEditor(") + 5200]
    ck("pane.classList.toggle('km-editing', !!LN.editing)" in ed,
       "lnRenderEditor() 幂等同步编辑态（换线 / 切模式不走 lnWide 也对齐）")
    ck("host.hidden = !LN.editing" in ed and "host.innerHTML = ''" in ed,
       "收起编辑时编辑区确实清空并隐藏")

    print("[2] 编辑区位置：紧跟标题，不再被线路资料推到下面")
    # app.js 里有多处 det.innerHTML（车站 / 目录 / OSM 列表…），取线路详情那段
    det = ""
    pos = 0
    while True:
        pos = js.find("det.innerHTML = `", pos)
        if pos < 0:
            break
        seg = js[pos:pos + 900]
        if 'id="lnKmBox"' in seg:
            det = seg
            break
        pos += 1
    p_km = det.find('id="lnKmBox"')
    p_head = det.find('id="lnHead"')
    p_info = det.find('id="lnInfo"')
    p_stops = det.find('id="lnStops"')
    ck(all(x >= 0 for x in (p_head, p_km, p_info, p_stops)), "详情五个区块都在")
    ck(p_head < p_km < p_info < p_stops,
       "顺序：标题 → 编辑区 → 线路资料 → 站序（编辑区在首屏）")

    print("[3] 编辑区内部：工具条 / 总里程 / 段距 / 折叠说明")
    ck('class="lnkm-bar"' in ed, "工具条改成独立的 lnkm-bar")
    ck("lnkm-head" not in ed, "旧的 lnkm-head 一坨已拆掉")
    ck('id="lnHelpBtn"' in ed and 'id="lnKmHelp"' in ed, "说明按钮 + 可折叠说明块都在")
    ck("LN.kmHelp" in ed, "说明展开状态在重渲染（切段距/累计）后保留")
    ck(ed.find('id="lnTotal"') < ed.find('id="lnSegs"'),
       "总里程输入排在段距列表上面（不用滚到底才能填）")
    ck(ed.find('id="lnSegs"') < ed.find('id="lnKmHelp"'),
       "长说明排在段距列表下面且默认折叠")
    ck('${LN.kmHelp ? \'\' : \' hidden\'}' in ed, "说明默认 hidden")
    for ident in ("lnSave", "lnResetAll", "lnCancel", "lnTotal", "lnTotalClr",
                  "lnSum", "lnWarn", "lnMsg", "lnSegs", "lnMode"):
        ck('id="%s"' % ident in ed, "保留既有 id #%s" % ident)
    ck("灰字是档案值" in ed and "按累计填写时" in ed, "两种模式的填写规则都还在")
    ck("保存后车次详情、地图、时间轴全部同步" in ed, "保存影响范围说明没丢")
    ck("填「-」" in ed and "不详" in ed, "「-」自动补 / 「不详」口径没被简化掉")

    print("[4] 里程算法与保存链路没被版面改动碰坏")
    for fn in ("function lnRenderSegs(", "function lnRenderCum(",
               "function lnRefreshPreview(", "function lnMarkSave(",
               "function lnSave(", "function lnResetAll("):
        ck(fn in js, "保留 %s" % fn.rstrip("("))
    ck("addEventListener('click', lnSave)" in ed, "保存按钮仍绑 lnSave")
    ck("addEventListener('click', lnResetAll)" in ed, "恢复档案按钮仍绑 lnResetAll")

    print("[5] CSS：编辑态收清单 / 藏站序 / 工具条吸顶 / 段距按视口高")
    for sel in ("#paneNet.km-editing #netConfirmedList{max-height:none",
                "#paneNet.km-editing #netConfirmedList .cf-row",
                "#paneNet.km-editing #lnStops{display:none}",
                "#paneNet.km-editing #cfToggle{display:none}"):
        ck(sel in css, "有规则 %s" % sel[:52])
    ck("编辑里程中已收起" in css, "清单表头写明编辑中已收起")
    ck("NET.cfOpen" in css or "NET.cfOpen" in js,
       "注明不动用户折叠状态（NET.cfOpen）")
    ck(".lnkm-bar{display:flex" in css and "position:sticky" in css,
       "工具条 flex + sticky，滚动时保存按钮不跑")
    ck("max-height:min(38vh,360px)" in css, "段距列表按视口高度自适应（不再写死 296px）")
    ck("@media(max-height:760px)" in css, "矮屏另给更矮的段距列表")
    ck(".lnkm-help[hidden]{display:none}" in css, "折叠说明确实隐藏")
    ck(".lnkm-head{" not in css, "旧 lnkm-head 样式已移除")
    ck(".side.wide{width:560px" in css, "侧栏放宽样式仍在")
    ck('id="netConfirmedList"' in html, "常驻清单仍是独立容器，编辑态只是 CSS 折叠")

    print("[6] HTTP 冒烟（8686 没起就跳过）")
    base = "http://127.0.0.1:8686"
    try:
        with urllib.request.urlopen(base + "/app.js", timeout=4) as r:
            live_js = r.read().decode("utf-8", "replace")
        ck("km-editing" in live_js, "服务返回的 app.js 已是新版")
        with urllib.request.urlopen(base + "/style.css", timeout=4) as r:
            live_css = r.read().decode("utf-8", "replace")
        ck("#paneNet.km-editing" in live_css, "服务返回的 style.css 已是新版")
        with urllib.request.urlopen(
                base + "/api/line/confirmed?full=1", timeout=6) as r:
            d = json.load(r)
        ck(isinstance(d.get("items"), list) and len(d["items"]) > 0,
           "确认清单接口仍返回 %d 条" % len(d.get("items") or []))
    except Exception as e:
        print("  SKIP 8686 未就绪（%s），静态检查已覆盖" % type(e).__name__)

    print("\n通过 %d 项，失败 %d 项" % (OK, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
