"""部署验收：按 DEPLOY.md 的清单逐项打真实接口，输出 PASS/FAIL。

用法：
    python scripts/acceptance.py http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理


def get(path: str, timeout: float = 30.0):
    request = urllib.request.Request(f"{BASE}{path}", headers={"Accept": "application/json"})
    with OPEN.open(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            return response.status, json.loads(raw.decode("utf-8")), content_type
        return response.status, raw, content_type


def check(name: str, fn) -> bool:
    try:
        detail = fn()
        print(f"[通过] {name}：{detail}")
        return True
    except Exception as exc:
        print(f"[失败] {name}：{exc}")
        return False


def main() -> int:
    print(f"验收目标 {BASE}")
    results = []

    def health():
        status, body, _ = get("/api/health")
        assert status == 200, f"HTTP {status}"
        assert body["database"]["ok"], f"数据库异常：{body['database']}"
        assert body["vector_store"]["ok"], f"向量库异常：{body['vector_store']}"
        return f"status={body['status']}，database/vector_store 均 ok"

    def documents():
        status, body, _ = get("/api/documents")
        assert status == 200, f"HTTP {status}"
        items = body["items"] if isinstance(body, dict) else body
        ready = [item for item in items if item.get("status") == "ready"]
        assert ready, "没有 ready 文档"
        return f"{len(items)} 份文档，其中 ready {len(ready)} 份"

    def search():
        # /api/search 搜的是会话与消息，不搜文档正文；用真实历史里的词才验得出来
        _, conversations, _ = get("/api/conversations")
        items = conversations["items"] if isinstance(conversations, dict) else conversations
        assert items, "没有历史会话可用于搜索验证"
        conversation_id = items[0]["id"]
        _, messages, _ = get(f"/api/conversations/{conversation_id}/messages")
        text = next((m["content"] for m in messages if m["role"] == "user"), "")
        # 取一个完整词（英文单词或 2–4 个汉字），避免「DPO和」这种跨词片段
        import re

        keyword = next(
            (word for word in re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fa5]{2,4}", text)),
            "",
        )
        assert keyword, "历史里没有可用的用户消息"
        status, body, _ = get(f"/api/search?q={urllib.parse.quote(keyword)}")
        assert status == 200, f"HTTP {status}"
        hits = len(body.get("conversations", [])) + len(body.get("messages", []))
        assert hits > 0, f"搜索「{keyword}」没有命中"
        return f"关键词「{keyword}」命中 {hits} 条（会话 {len(body.get('conversations', []))} / 消息 {len(body.get('messages', []))}）"

    def graph():
        status, body, _ = get("/api/graph/explore?limit=20")
        assert status == 200, f"HTTP {status}"
        return f"{len(body['nodes'])} 节点 / {len(body['edges'])} 边"

    def chunk_context():
        _, graph_body, _ = get("/api/graph/explore?limit=20")
        chunk_id = next(
            (node["data"].get("chunkId") for node in graph_body["nodes"] if node["data"].get("chunkId")),
            None,
        )
        assert chunk_id, "图谱节点没有 chunkId"
        status, body, _ = get(f"/api/chunks/{chunk_id}/context")
        assert status == 200, f"HTTP {status}"
        assert len(body["text"]) > 200, "上下文过短"
        return f"整节 {len(body['text'])} 字，{body['location']}"

    def page():
        request = urllib.request.Request(f"{BASE}/")
        with OPEN.open(request, timeout=30) as response:
            html = response.read().decode("utf-8", "ignore")
        assert response.status == 200, f"HTTP {response.status}"
        assert "<div id=\"root\">" in html or "root" in html, "首页不是前端入口"
        return f"{len(html)} 字节"

    def stream_chat():
        payload = json.dumps({"message": "DPO 和 KTO 有什么区别", "mode": "knowledge"}).encode()
        request = urllib.request.Request(
            f"{BASE}/api/chat/stream",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        started = time.time()
        statuses = []
        with OPEN.open(request, timeout=180) as response:
            assert response.status == 200, f"HTTP {response.status}"
            for raw in response:
                line = raw.decode("utf-8", "ignore")
                if line.startswith("event:"):
                    statuses.append(line.split(":", 1)[1].strip())
        elapsed = time.time() - started
        assert "done" in statuses, f"事件流缺少 done：{statuses[:6]}"
        assert "delta" in statuses, "事件流没有增量文本"
        return f"{len(statuses)} 个事件，{elapsed:.1f}s"

    results.append(check("健康检查", health))
    results.append(check("文档列表", documents))
    results.append(check("全文搜索", search))
    results.append(check("图谱数据", graph))
    results.append(check("整节上下文", chunk_context))
    results.append(check("前端入口", page))
    results.append(check("流式问答", stream_chat))

    passed = sum(1 for item in results if item)
    print(f"\n[结果] {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
