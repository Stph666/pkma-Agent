"""按视图截图：python scripts/ui_shot.py <输出> [宽] [高] [导航名] [问题]"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_shot  # noqa: E402  复用浏览器启动逻辑


async def run_all(nav: str, question: str, out: Path, width: int, height: int, reader: str = "-") -> None:
    import json
    import time as _time
    import urllib.request

    import websockets

    ws_url = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{chat_shot.PORT}/json/list", timeout=1) as resp:
                pages = [item for item in json.load(resp) if item.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            pass
        _time.sleep(0.3)

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        counter = 0

        async def call(method, params=None):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == counter:
                    return msg.get("result", {})

        async def ev(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("value")

        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": chat_shot.URL})
        await asyncio.sleep(8)

        if nav and nav != "-":
            await ev(
                "(() => { const labels=[...document.querySelectorAll('.nav-radio label')];"
                f"const t=labels.find(l=>l.innerText.trim()==='{nav}'); if(t) t.click(); return !!t; }})()"
            )
            await asyncio.sleep(4)

        if question and question != "-":
            await ev(
                "(() => { const b=document.querySelector('.composer-row textarea');"
                "const s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
                f"s.call(b, {json.dumps(question, ensure_ascii=False)});"
                "b.dispatchEvent(new Event('input',{bubbles:true})); return 1; })()"
            )
            await asyncio.sleep(1)
            await ev("document.querySelector('.send-btn').click()")
            for _ in range(40):
                await asyncio.sleep(1.5)
                text = await ev("(() => { const el=document.querySelector('.status-line'); return el?el.innerText:''; })()")
                if "完成" in (text or "") or "没有足够依据" in (text or "") or "失败" in (text or ""):
                    break

        if reader and reader != "-":
            await ev("(() => { const a=document.querySelector('.src-open'); if(a) a.click(); return 1; })()")
            await asyncio.sleep(1)

        await asyncio.sleep(2)
        result = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        import base64
        out.write_bytes(base64.b64decode(result["data"]))


def main() -> int:
    out = Path(sys.argv[1])
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1366
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 768
    nav = sys.argv[4] if len(sys.argv) > 4 else "-"
    question = sys.argv[5] if len(sys.argv) > 5 else "-"
    reader = sys.argv[6] if len(sys.argv) > 6 else "-"

    import subprocess
    profile = Path("/tmp/edge-ui-profile")
    subprocess.run(["rm", "-rf", str(profile)], check=False)
    process = subprocess.Popen(
        [chat_shot.EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={chat_shot.PORT}", f"--user-data-dir={profile}",
         f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(run_all(nav, question, out, width, height, reader))
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
