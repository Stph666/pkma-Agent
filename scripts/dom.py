"""在真实浏览器里求值：python scripts/dom.py <url> "<js 表达式>" [宽] [高] [等待秒]"""

from __future__ import annotations

import asyncio
import json
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path

EDGE = os.environ.get("EDGE_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
PORT = 9334


async def main() -> int:
    url = sys.argv[1]
    expression = sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1366
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 768
    wait = float(sys.argv[5]) if len(sys.argv) > 5 else 7.0

    profile = Path("/tmp/edge-dom-profile")
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
                    pages = [item for item in json.load(response) if item.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if ws_url is None:
            print("无法连接浏览器", file=sys.stderr)
            return 1

        import websockets

        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
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
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False
            })
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(wait)
            result = await call("Runtime.evaluate", {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            })
            value = result.get("result", {})
            print(json.dumps(value.get("value", value), ensure_ascii=False, indent=2))
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
