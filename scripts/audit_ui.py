"""界面回归探针：把审查里「量出来的数」变成每次能重跑的门禁。

为什么需要它：`docs/REVIEW.md` 的 32 条缺陷里有 9 条是界面问题，修的时候全靠
`getBoundingClientRect` 量出来。但量完就过去了——代码一改，没人知道有没有退回去。
截图只能回答「像不像」，这里断言的是「对不对」：元素在不在、宽度够不够、有没有盖住。

每条检查都对应审查文档里的一个编号，失败时能直接翻回去看当时为什么这么修。

用法（先起预览服务）：
    .venv/bin/python scripts/audit_ui.py --port 8791
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shot import Cdp, Ws, launch, page_socket, settle  # noqa: E402

VIEW_W, VIEW_H = 420, 620          # 真实胶囊窗口尺寸，所有检查默认在这个视口里做


def goto(cdp: Cdp, base: str, path: str, width: int = VIEW_W, height: int = VIEW_H) -> None:
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=1, mobile=False)
    cdp.call("Page.navigate", url=base + path)
    cdp.wait("document.readyState === 'complete'")
    cdp.wait("typeof render === 'function'")


def rect(cdp: Cdp, selector: str) -> dict:
    """量一个元素的盒子和它跟内容的关系（sw/cw 用来判断有没有被裁）。"""
    raw = cdp.js(f"""(() => {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return JSON.stringify({{missing: true}});
        const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
        return JSON.stringify({{
            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            w: r.width, h: r.height, display: cs.display, hidden: el.hidden,
            sw: el.scrollWidth, cw: el.clientWidth,
            shadow: cs.boxShadow, bg: cs.backgroundColor
        }});
    }})()""")
    return json.loads(raw)


def fake_history(cdp: Cdp, rows: list[dict]) -> None:
    """把 /api/history 换成固定数据再走真实渲染路径。

    直接往 DOM 里塞行会绕过模板，测出来的宽度没有意义；这里只换数据源，
    `loadRecent()` 和它用的样式都是线上那一套。
    """
    cdp.js(f"""(() => {{
        const real = window.api;
        window.api = (url, opt) => String(url).includes('/api/history')
            ? Promise.resolve({json.dumps(rows, ensure_ascii=False)})
            : real(url, opt);
    }})()""")


def check_hidden(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#19：`.ask{display:flex}` 曾经盖掉 `hidden` 属性，待录入时死输入框藏不住。"""
    goto(cdp, base, "/?panel=status&still=1")
    cdp.js("new Promise(ok => setTimeout(() => { setOnboard(true); ok(true); }, 700))")
    settle(cdp)
    box = rect(cdp, "#ask-row")
    ok = box.get("display") == "none"
    return ok, f"hidden={box.get('hidden')} display={box.get('display')}"


def check_tag(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#20：340px 窄面板下路由标签被 `text-overflow` 裁掉过半个。"""
    goto(cdp, base, "/?panel=recent&still=1", width=340)
    fake_history(cdp, [{"ts": "14:02", "route": "fast",
                        "text": "Siri，检查一下服务器现在的负载和磁盘使用情况",
                        "reply": "负载 0.42"}])
    cdp.js("new Promise(ok => { switchTab('recent').then(() => loadRecent().then(ok)); })")
    settle(cdp)
    box = rect(cdp, ".row .tag")
    if box.get("missing"):
        return False, "没有找到路由标签"
    ok = box["sw"] <= box["cw"] + 1
    return ok, f"标签 {box['cw']:.0f}px 装 {box['sw']:.0f}px 内容"


def check_dirty(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#21：20 个字段滚到中段，得能看出「有未保存改动」。"""
    goto(cdp, base, "/settings?still=1")
    cdp.wait("typeof loadSettings === 'function'")
    time.sleep(0.6)
    cdp.js("""(() => { const el = document.querySelector('#f-audio-vad_threshold');
        el.value = '0.86'; el.dispatchEvent(new Event('input')); })()""")
    settle(cdp)
    state = json.loads(cdp.js("""(() => JSON.stringify({
        save: !document.getElementById('save').disabled,
        reset: !document.getElementById('reset').disabled,
        dot: document.querySelector('[data-tab="settings"]').classList.contains('dirty')
    }))()"""))
    ok = state["save"] and state["dot"]
    return ok, f"保存可用={state['save']} 页签带点={state['dot']} 放弃可用={state['reset']}"


def check_hover(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#22：`:hover` 被 `[data-state]` 的边框色盖掉过，鼠标移上去毫无反应。"""
    goto(cdp, base, "/?panel=status&still=1")
    cdp.js("new Promise(ok => setTimeout(() => {"
           "render({state:'listening', label:'聆听中'}); ok(true); }, 700))")
    settle(cdp)
    before = rect(cdp, "#pill")
    point = json.loads(cdp.js("""(() => { const r = document.getElementById('pill')
        .getBoundingClientRect();
        return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2}); })()"""))
    cdp.call("Input.dispatchMouseEvent", type="mouseMoved", **point)
    settle(cdp)
    after = rect(cdp, "#pill")
    changed = (before["shadow"] != after["shadow"]) or (before["bg"] != after["bg"])
    return changed, f"shadow {before['shadow'][:24] or '无'} → {after['shadow'][:24] or '无'}"


def check_primary_width(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#26：三个按钮并排时主操作反而最窄（104px vs 154px），最不显眼。"""
    goto(cdp, base, "/settings?still=1")
    cdp.wait("typeof setUndo === 'function'")
    time.sleep(0.6)
    cdp.js("setUndo(true)")
    settle(cdp)
    widths = json.loads(cdp.js("""(() => { const w = id => Math.round(document
        .getElementById(id).getBoundingClientRect().width);
        return JSON.stringify({undo: w('undo'), reset: w('reset'), save: w('save')}); })()"""))
    ok = widths["save"] > widths["reset"] and widths["save"] > widths["undo"]
    return ok, "撤销 {undo} / 放弃 {reset} / 保存 {save}".format(**widths)


def check_undo_arms(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#28：撤销一点就退且不可逆，改成两段式——第一下只上膛，不发请求。"""
    goto(cdp, base, "/settings?still=1")
    cdp.wait("typeof setUndo === 'function'")
    time.sleep(0.6)
    cdp.js("""(() => { window.__calls = 0; setUndo(true);
        const real = window.api;
        window.api = (url, opt) => {
            if (String(url).includes('undo')) window.__calls++;
            return real(url, opt);
        }; })()""")
    cdp.js("document.getElementById('undo').click()")
    time.sleep(0.15)
    armed = cdp.js("document.getElementById('undo').textContent")
    calls = cdp.js("window.__calls")
    cdp.js("disarmUndo()")
    ok = armed == "确认撤销" and calls == 0
    return ok, f"第一下按钮变成「{armed}」，发出的撤销请求 {calls} 次"


def check_save_bar(cdp: Cdp, base: str) -> tuple[bool, str]:
    """#30：滚动容器有下内边距时，sticky 保存条下面会漏出 16px 正在滚的内容。"""
    goto(cdp, base, "/settings?still=1")
    cdp.wait("typeof loadSettings === 'function'")
    time.sleep(0.8)
    cdp.js("document.querySelector('.body').scrollTop = 99999")
    settle(cdp)
    bar, body = rect(cdp, "#actions"), rect(cdp, ".body")
    ok = abs(bar["bottom"] - body["bottom"]) <= 1
    return ok, f"保存条底 {bar['bottom']:.0f} / 滚动区底 {body['bottom']:.0f}"


def check_confirm_fits(cdp: Cdp, base: str) -> tuple[bool, str]:
    """确认卡是最后一道防线：在真实窗口里必须整张可见，不能被挤出去。"""
    goto(cdp, base, "/settings?still=1")
    cdp.js("render({state:'confirm', detail:'「把复杂重构委托给本地 Coding CLI」"
           "会真的改动东西，需要你点头'})")
    settle(cdp)
    card = rect(cdp, "#confirm")
    ok = not card.get("missing") and card["top"] >= 0 and card["bottom"] <= VIEW_H
    return ok, f"卡片 {card.get('top', -1):.0f}–{card.get('bottom', -1):.0f}，视口高 {VIEW_H}"


def reachable(base: str) -> bool:
    """先确认服务活着，否则后面每个检查都会以「连不上 Chrome」的面目失败。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


CHECKS = [
    ("#19 [hidden] 真的藏得住", check_hidden),
    ("#20 340px 下标签不被裁", check_tag),
    ("#21 有未保存改动看得出来", check_dirty),
    ("#22 hover 有可见反馈", check_hover),
    ("#26 主操作最宽", check_primary_width),
    ("#28 撤销第一下只上膛", check_undo_arms),
    ("#30 保存条下面不漏内容", check_save_bar),
    ("确认卡在真实窗口里完整可见", check_confirm_fits),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791, help="预览服务端口")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    if not reachable(base):
        print(f"❌ 连不上 {base} —— 先起服务：voxos serve --no-open")
        return 2

    chrome = launch(9361, Path("/tmp/voxos-audit-profile"))
    failed = 0
    try:
        cdp = Cdp(Ws(page_socket(9361)))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        for name, check in CHECKS:
            try:
                ok, detail = check(cdp, base)
            except Exception as exc:                       # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            failed += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {name:28s} {detail}")
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
    print("-" * 60)
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
