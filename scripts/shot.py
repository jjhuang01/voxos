"""像素走查用的截图工具：用 CDP 精确控制视口、注入状态、模拟 hover。

为什么不直接 `chrome --screenshot`：那条路只能截「加载完的整页」，
拿不到 hover / 焦点 / 需要 JS 才能进入的状态，而且无头 Chrome 的最小视口是
500px，截 340px 的窄布局会被拉宽成假象。CDP 可以指定精确视口再按元素裁切。

用法（先起预览服务）：
    .venv/bin/python scripts/shot.py --port 8791 --out docs/screenshots
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_MASK = 0x80
_FIN, _TEXT, _CLOSE, _PING, _PONG = 0x1, 0x1, 0x8, 0x9, 0xA


class Ws:
    """够用的 WebSocket 客户端：只做 CDP 需要的文本帧。"""

    def __init__(self, url: str) -> None:
        _, _, rest = url.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"WebSocket 握手失败：{head[:120]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            piece = self.sock.recv(4096)
            if not piece:
                raise RuntimeError("连接被关闭")
            buf += piece
        return buf

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([_MASK | _FIN | _TEXT])
        size = len(payload)
        if size < 126:
            header.append(_MASK | size)
        elif size < 1 << 16:
            header.append(_MASK | 126)
            header += struct.pack("!H", size)
        else:
            header.append(_MASK | 127)
            header += struct.pack("!Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _exactly(self, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            piece = self.sock.recv(count - len(buf))
            if not piece:
                raise RuntimeError("连接被关闭")
            buf += piece
        return buf

    def recv(self) -> str:
        """读一条完整消息（自动拼接分片、忽略 ping/pong）。"""
        chunks: list[bytes] = []
        while True:
            first, second = self._exactly(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._exactly(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exactly(8))[0]
            body = self._exactly(size) if size else b""
            if opcode == _PING:
                continue
            if opcode == _CLOSE:
                raise RuntimeError("服务端关闭了连接")
            chunks.append(body)
            if first & _FIN:
                return b"".join(chunks).decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    def __init__(self, ws: Ws) -> None:
        self.ws = ws
        self.seq = 0

    def call(self, method: str, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self.seq:
                continue                      # 事件帧，跳过
            if "error" in message:
                raise RuntimeError(f"{method} 失败：{message['error']}")
            return message.get("result", {})

    def js(self, expression: str):
        """执行一段 JS 并返回它的值（同步等待 Promise）。"""
        result = self.call("Runtime.evaluate", expression=expression,
                           awaitPromise=True, returnByValue=True)
        return result.get("result", {}).get("value")

    def wait(self, expression: str, timeout: float = 6.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression):
                return
            time.sleep(0.05)
        raise RuntimeError(f"等待超时：{expression}")


def launch(port: int, profile: Path) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _local() -> urllib.request.OpenerDirector:
    """访问 127.0.0.1 必须绕开系统代理，否则拿到的是代理的 502。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def page_socket(port: int, timeout: float = 20.0) -> str:
    opener, deadline = _local(), time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/json", timeout=3) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page":
                    return target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("连不上 Chrome 调试端口")


def settle(cdp: Cdp, frames: int = 2) -> None:
    """逼出几帧再截图。

    headless Chrome 不主动产帧，而 CSS transition 靠帧推进——不逼帧的话过渡会永远停在
    起始值：`getComputedStyle` 读到的是改之前的样子，截出来的图也是。这里用最便宜的
    jpeg 截两帧把动画时钟推过去，正式那一张才是落定的状态。
    """
    for _ in range(frames):
        cdp.call("Page.captureScreenshot", format="jpeg", quality=1)
        time.sleep(0.05)


def shot(cdp: Cdp, spec: dict, base: str, out: Path) -> Path:
    """进入一个状态并截图，按 .stage 的实际高度裁切。"""
    # headless Chrome 默认报 `prefers-reduced-motion: reduce`（实测 matchMedia 为 true），
    # 不显式关掉的话，截出来的图永远没有呼吸点/电平条——「动效对不对」等于没看过。
    cdp.call("Emulation.setEmulatedMedia",
             features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
    cdp.call("Emulation.setDeviceMetricsOverride", width=spec.get("width", 420),
             height=spec.get("height", 900), deviceScaleFactor=2, mobile=False)
    cdp.call("Page.navigate", url=base + spec["path"])
    cdp.wait("document.readyState === 'complete'")
    cdp.wait("typeof render === 'function'")
    if spec.get("setup"):
        cdp.js(spec["setup"])
        time.sleep(0.25)
    if spec.get("hover"):
        box = cdp.js(f"""(() => {{
            const r = document.querySelector({json.dumps(spec['hover'])}).getBoundingClientRect();
            return JSON.stringify({{x: r.x + r.width / 2, y: r.y + r.height / 2}});
        }})()""")
        point = json.loads(box)
        cdp.call("Input.dispatchMouseEvent", type="mouseMoved", **point)
        time.sleep(0.2)
    settle(cdp)
    clip = cdp.js("""(() => {
        const stage = document.querySelector('.stage').getBoundingClientRect();
        const toast = document.getElementById('toast');
        const shown = toast.classList.contains('show') ? toast.getBoundingClientRect().bottom : 0;
        return JSON.stringify({x: 0, y: 0, width: document.documentElement.clientWidth,
                               height: Math.ceil(Math.max(stage.bottom, shown) + 12)});
    })()""")
    data = cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                    clip=dict(json.loads(clip), scale=1))["data"]
    target = out / f"{spec['name']}.png"
    target.write_bytes(base64.b64decode(data))
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791, help="预览服务端口")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--only", default="", help="只跑名字里含此子串的镜头")
    args = parser.parse_args()
    base, out = f"http://127.0.0.1:{args.port}", Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    shots = [s for s in SPECS if args.only in s["name"]]
    profile = Path("/tmp/voxos-shot-profile")
    chrome = launch(9333, profile)
    try:
        cdp = Cdp(Ws(page_socket(9333)))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        for spec in shots:
            print(f"  ✓ {shot(cdp, spec, base, out)}")
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
    print(f"{len(shots)} 张截图写入 {out}")
    return 0


# 每个镜头：进哪个 URL、要不要 JS 造状态、要不要 hover。
# setup 里调用的都是页面自己的函数（render / open / setOnboard），
# 走的是真实渲染路径，不是另画一张假图。setup 可以返回 Promise，会被等待。
SPECS = [
    # 首屏会拉一次 /state 并按服务端事实重绘，等它落地再摆姿势，否则会被它覆盖回去
    {"name": "status", "path": "/?panel=status&still=1",
     "setup": "new Promise(ok => setTimeout(() => {"
              "render({state:'listening', label:'聆听中'}); ok(true); }, 700))"},
    {"name": "status-hover", "path": "/?panel=status&still=1", "hover": "#pill",
     "setup": "new Promise(ok => setTimeout(() => {"
              "render({state:'listening', label:'聆听中'}); ok(true); }, 700))"},
    {"name": "recent", "path": "/?panel=recent&still=1",
     "setup": "switchTab('recent')"},
    {"name": "confirm", "path": "/?panel=status&still=1",
     "setup": "render({state:'confirm', detail:'「把复杂重构委托给本地 Coding CLI」"
              "会真的改动东西，需要你点头'})"},
    # 真实胶囊窗口就是 420×620：确认卡 + 满高面板必须同时装得下
    {"name": "confirm-620", "path": "/settings?still=1", "height": 620,
     "setup": "render({state:'confirm', detail:'「把复杂重构委托给本地 Coding CLI」"
              "会真的改动东西，需要你点头'})"},
    {"name": "onboard", "path": "/?panel=status&still=1",
     # 等首屏状态落地再切待录入态，否则随后的 loadStatus 会按服务端事实把卡片收回去。
     # 这台预览服务其实已经录过声纹，所以顺手把「声纹」格也改成未录入，避免图文自相矛盾。
     "setup": "new Promise(ok => setTimeout(() => {"
              "pill.dataset.state = 'onboard'; label.textContent = '待设置';"
              "const v = document.querySelectorAll('.cell .v')[0];"
              "v.textContent = '未录入'; v.className = 'v warn';"
              "setOnboard(true); ok(true); }, 700))"},
    {"name": "settings-focus", "path": "/settings?still=1",
     "setup": "document.querySelector('#f-audio-vad_threshold').focus()"},
    {"name": "settings-dirty", "path": "/settings?still=1",
     "setup": "(() => { const el = document.querySelector('#f-audio-vad_threshold');"
              "el.value = '0.86'; el.dispatchEvent(new Event('input'));"
              "document.querySelector('.body').scrollTop = 99999; })()"},
    # 有备份时保存条多出「撤销」：三个按钮在 400px 里怎么分宽度，得量出来看
    {"name": "settings-undo", "path": "/settings?still=1",
     "setup": "(() => { setUndo(true); document.querySelector('.body').scrollTop = 99999; })()"},
    {"name": "narrow", "path": "/?panel=recent&still=1", "width": 340,
     "setup": "switchTab('recent')"},
]

if __name__ == "__main__":
    sys.exit(main())
