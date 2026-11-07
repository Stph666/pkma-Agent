"""图谱接口集成测试：排序、筛选、边合并与采样说明。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import check_database

DB_OK, DB_DETAIL = check_database()
pytestmark = pytest.mark.skipif(not DB_OK, reason=f"MySQL 不可用：{DB_DETAIL}")


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def has_graph(client: TestClient) -> bool:
    payload = client.get("/api/graph", params={"limit": -1}).json()
    return bool(payload["total_entities"])


def test_graph_response_shape(client: TestClient):
    payload = client.get("/api/graph", params={"limit": 10}).json()

    assert {
        "nodes",
        "edges",
        "total_entities",
        "shown_entities",
        "total_relations",
        "shown_relations",
        "hidden_isolated",
        "truncated",
        "sampled_chunks",
        "total_chunks",
    } <= set(payload)
    for node in payload["nodes"]:
        assert {"id", "name", "type", "document_id", "filename", "degree"} <= set(node)


def test_nodes_sorted_by_degree(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    nodes = client.get("/api/graph", params={"limit": -1}).json()["nodes"]
    degrees = [node["degree"] for node in nodes]
    assert degrees == sorted(degrees, reverse=True), "节点应按关联度降序"


def test_limit_truncates_by_degree_not_by_name(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    full = client.get("/api/graph", params={"limit": -1}).json()
    limited = client.get("/api/graph", params={"limit": 3}).json()

    assert limited["shown_entities"] == 3
    assert limited["truncated"] is True
    # 截断必须保留关联度最高的那几个
    expected = [node["name"] for node in full["nodes"][:3]]
    assert [node["name"] for node in limited["nodes"]] == expected


def test_auto_limit_adapts(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    auto = client.get("/api/graph", params={"limit": 0}).json()
    if auto["total_entities"] <= 40:
        assert auto["truncated"] is False
    else:
        assert auto["shown_entities"] == 40


def test_isolated_nodes_hidden_by_default(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    default = client.get("/api/graph", params={"limit": -1}).json()
    with_isolated = client.get("/api/graph", params={"limit": -1, "include_isolated": True}).json()

    assert with_isolated["shown_entities"] >= default["shown_entities"]
    assert with_isolated["shown_entities"] - default["shown_entities"] == default["hidden_isolated"]
    assert all(node["degree"] > 0 for node in default["nodes"])


def test_min_degree_and_type_filters(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    high = client.get("/api/graph", params={"limit": -1, "min_degree": 2}).json()
    assert all(node["degree"] >= 2 for node in high["nodes"])

    types = client.get("/api/graph", params={"limit": -1, "types": ["技术"]}).json()
    assert all(node["type"] == "技术" for node in types["nodes"])


def test_edges_only_between_visible_nodes(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    payload = client.get("/api/graph", params={"limit": 10}).json()
    visible = {node["id"] for node in payload["nodes"]}
    for edge in payload["edges"]:
        assert edge["source"] in visible
        assert edge["target"] in visible
        assert edge["relation_count"] >= 1
        assert edge["label"]


def test_sampling_info_reported(client: TestClient, has_graph):
    if not has_graph:
        pytest.skip("当前库没有图谱数据")

    payload = client.get("/api/graph", params={"limit": 10}).json()
    assert payload["total_chunks"] > 0
    assert payload["sampled_chunks"] <= payload["total_chunks"]


def test_extract_plan_returns_three_depths(client: TestClient):
    docs = client.get("/api/documents").json()["items"]
    ready = [item for item in docs if item["status"] == "ready"]
    if not ready:
        pytest.skip("没有可用文档")

    plan = client.get("/api/graph/extract-plan", params={"document_id": ready[0]["id"]}).json()
    assert {item["depth"] for item in plan["plans"]} == {"fast", "standard", "deep"}
    ordered = sorted(plan["plans"], key=lambda item: item["chunks"])
    assert ordered[0]["chunks"] <= ordered[1]["chunks"] <= ordered[2]["chunks"]
