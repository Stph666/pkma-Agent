"""用真实鼠标事件点图谱节点并截图，验证详情面板（合成的 MouseEvent 进不了 cytoscape）。"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path

EDGE = os.environ.get("EDGE_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
PORT = 9334


async def main(url: str, out: Path, width: int = 1600, height: int = 900) -> int:
    profile = Path("/tmp/edge-click-profile")
    subprocess.run(["rm", "-rf", str(profile)], check=False)
    process = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1) as response:
                    pages = [item for item in json.load(response) if item.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not ws_url:
            print("无法连接调试端口", file=sys.stderr)
            return 1

        import websockets

        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            counter = 0

            async def call(method: str, params: dict | None = None) -> dict:
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == counter:
                        return message.get("result", {})

            await call("Page.enable")
            await call("Runtime.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(6)

            await call("Runtime.evaluate", {"expression": """
                [...document.querySelectorAll('.nav-item')].find(b=>b.innerText.includes('图谱')).click();
            """})
            await asyncio.sleep(5)

            result = await call("Runtime.evaluate", {"returnByValue": True, "expression": """
                (() => {
                  const f = document.querySelector('iframe');
                  const r = f.getBoundingClientRect();
                  const w = f.contentWindow;
                  const cy = w.cy;
                  const cr = cy.container().getBoundingClientRect();
                  const nodes = cy.nodes().sort((a,b)=>b.data('degree')-a.data('degree'));
                  let node = null, p = null;
                  for (const n of nodes) {
                    const pos = n.renderedPosition();
                    if (pos.x > 40 && pos.y > 40 && pos.x < cr.width - 40 && pos.y < cr.height - 40) {
                      node = n; p = pos; break;
                    }
                  }
                  if (!node) return null;
                  return {x: r.left + cr.left + p.x, y: r.top + cr.top + p.y, name: node.data('label')};
                })()
            """})
            point = result["result"]["value"]
            if not point:
                print("没有可点击的节点")
                return 1
            print("点击节点:", point["name"], round(point["x"]), round(point["y"]))

            for kind in ("mousePressed", "mouseReleased"):
                await call("Input.dispatchMouseEvent", {
                    "type": kind, "x": point["x"], "y": point["y"],
                    "button": "left", "clickCount": 1,
                })
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.6)

            info = await call("Runtime.evaluate", {"returnByValue": True, "expression": """
                (() => {
                  const d = document.querySelector('iframe').contentDocument;
                  const info = d.getElementById('info');
                  return {
                    display: getComputedStyle(info).display,
                    title: info.querySelector('h4').textContent,
                    meta: info.querySelector('.info-meta').textContent,
                    bodyLen: info.querySelector('.info-body').textContent.length,
                    hasNewline: info.querySelector('.info-body').textContent.includes('\\n'),
                    link: info.querySelector('.info-open').getAttribute('href'),
                  };
                })()
            """})
            print(json.dumps(info["result"]["value"], ensure_ascii=False, indent=2))

            shot = await call("Page.captureScreenshot", {"format": "png"})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"已保存 {out}")
            return 0
    finally:
        process.terminate()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1], Path(sys.argv[2]))))
