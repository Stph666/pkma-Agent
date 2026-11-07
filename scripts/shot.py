"""用 Chrome DevTools Protocol 截图：等待页面真正渲染后再抓图。

用法：python scripts/shot.py <url> <输出路径> [宽] [高] [等待秒数]
"""

from __future__ import annotations

import base64
import json
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path

EDGE = os.environ.get("EDGE_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
PORT = 9333


def main() -> int:
    url = sys.argv[1]
    out = Path(sys.argv[2])
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1366
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 768
    wait = float(sys.argv[5]) if len(sys.argv) > 5 else 6.0
    script = sys.argv[6] if len(sys.argv) > 6 else ""

    out.parent.mkdir(parents=True, exist_ok=True)
    profile = Path("/tmp/edge-shot-profile")
    subprocess.run(["rm", "-rf", str(profile)], check=False)

    process = subprocess.Popen(
        [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1) as response:
                    targets = json.load(response)
                pages = [item for item in targets if item.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if ws_url is None:
            print("无法连接浏览器调试端口", file=sys.stderr)
            return 1

        import asyncio

        import websockets

        async def capture() -> None:
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
                await call("Emulation.setDeviceMetricsOverride", {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                })
                await call("Page.navigate", {"url": url})
                await asyncio.sleep(wait)
                if script:
                    await call("Runtime.evaluate", {"expression": script, "awaitPromise": True})
                    await asyncio.sleep(2.5)
                result = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
                out.write_bytes(base64.b64decode(result["data"]))

        asyncio.run(capture())
        print(f"已保存 {out}（{out.stat().st_size // 1024} KB）")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
