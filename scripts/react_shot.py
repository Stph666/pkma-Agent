"""在 React 版界面里真实输入并按回车，等待回答后截图。

用法：python scripts/react_shot.py "<问题>" <输出路径> [宽] [高]
"""

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
PORT = 9336
URL = "http://127.0.0.1:8000/"


async def run(question: str, out: Path, width: int, height: int) -> None:
    import websockets

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
        raise RuntimeError("无法连接浏览器")

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

        async def ev(expression: str):
            result = await call("Runtime.evaluate", {
                "expression": expression, "returnByValue": True, "awaitPromise": True,
            })
            return result.get("result", {}).get("value")

        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })
        await call("Page.navigate", {"url": URL})
        await asyncio.sleep(4)

        await ev("document.querySelector('.composer textarea').focus()")
        await asyncio.sleep(0.3)
        await call("Input.insertText", {"text": question})
        await asyncio.sleep(0.4)

        await asyncio.sleep(0.4)
        await ev("(() => { const btn = document.querySelector('.btn-send'); if (btn && !btn.disabled) btn.click(); return true; })()")

        for _ in range(40):
            await asyncio.sleep(1.2)
            done = await ev(
                "(() => { const a = document.querySelector('.answer-body');"
                " const caret = document.querySelector('.caret');"
                " const cards = document.querySelectorAll('.src').length;"
                " return !!a && !caret && cards > 0; })()"
            )
            if done:
                break

        await asyncio.sleep(1.2)
        result = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        out.write_bytes(base64.b64decode(result["data"]))


def main() -> int:
    question = sys.argv[1]
    out = Path(sys.argv[2])
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1366
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 768

    out.parent.mkdir(parents=True, exist_ok=True)
    profile = Path("/tmp/edge-react-profile")
    subprocess.run(["rm", "-rf", str(profile)], check=False)
    process = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        asyncio.run(run(question, out, width, height))
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
