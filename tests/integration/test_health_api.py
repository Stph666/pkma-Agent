"""集成测试：真实 MySQL 下的健康检查与建表验证。

未启动 MySQL 时自动跳过，避免把环境问题误报为代码缺陷。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db import check_database, get_engine, table_names

DB_OK, DB_DETAIL = check_database()
pytestmark = pytest.mark.skipif(not DB_OK, reason=f"MySQL 不可用：{DB_DETAIL}")


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["database"]["ok"] is True
    assert body["app_name"]
    assert "secret" not in response.text.lower()


def test_tables_created(client: TestClient):
    existing = set(inspect(get_engine()).get_table_names())
    assert set(table_names()).issubset(existing)


def test_react_ui_served_at_root(client: TestClient):
    """根路径是新版 React 前端，Gradio 退到 /classic 作为兜底。"""
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="root"' in response.text

    legacy = client.get("/classic/")
    assert legacy.status_code == 200
    assert "个人知识管理智能体" in legacy.text


def test_api_docs_available(client: TestClient):
    assert client.get("/api/openapi.json").status_code == 200


def test_unknown_api_returns_json(client: TestClient):
    response = client.get("/api/not-exists")
    assert response.status_code == 404
