"""批量审计接口 POST /api/v1/audits/batch 的端到端测试。"""

FEASIBLE_MANIFEST = {
    "plugin_name": "megaplugin",
    "plugin_version": "1.0.0",
    "dependencies": [],
}
INFEASIBLE_MANIFEST = {
    "dependencies": [
        {"name": "cachekit", "range": "^1.0.0"},
        {"name": "loggerkit", "range": "^0.9.0"},
    ]
}


def test_batch_mixed_statuses_counts_order_and_persistence(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": [FEASIBLE_MANIFEST, INFEASIBLE_MANIFEST, FEASIBLE_MANIFEST]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 计数
    assert body["total"] == 3
    assert body["feasible"] == 2
    assert body["infeasible"] == 1
    assert body["inconclusive"] == 0
    assert body["error"] == 0
    assert body["counts"] == {
        "total": 3,
        "feasible": 2,
        "infeasible": 1,
        "inconclusive": 0,
        "error": 0,
    }

    # 顺序与下标与输入一致
    assert [item["index"] for item in body["results"]] == [0, 1, 2]
    statuses = [item["status"] for item in body["results"]]
    assert statuses == ["feasible", "infeasible", "feasible"]

    for item in body["results"]:
        assert item["error"] is None
        assert item["audit_id"]
        assert item["audit"]["id"] == item["audit_id"]
        assert item["audit"]["status"] == item["status"]
        # 成功项分别持久化，可通过原单条查询接口取回
        detail = client.get(f"/api/v1/audits/{item['audit_id']}").json()
        assert detail["id"] == item["audit_id"]
        assert detail["status"] == item["status"]

    # 列表接口也能看到全部三条
    listed = client.get("/api/v1/audits?limit=100").json()
    assert listed["total"] >= 3


def test_batch_invalid_range_item_does_not_block_others(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={
            "manifests": [
                {"dependencies": [{"name": "x", "range": "not-valid"}]},
                FEASIBLE_MANIFEST,
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["error"] == 1
    assert body["feasible"] == 1

    bad, good = body["results"]
    assert bad["index"] == 0
    assert bad["audit_id"] is None
    assert bad["status"] is None
    assert bad["audit"] is None
    assert bad["error"]["code"] == "invalid_manifest"
    assert bad["error"]["message"]
    # 结构化 Pydantic 错误：能定位到具体字段
    details = bad["error"]["details"]
    assert details
    assert details[0]["loc"][-1] == "range"

    assert good["index"] == 1
    assert good["status"] == "feasible"
    assert good["error"] is None


def test_batch_root_consistency_error_is_item_error(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": [{"plugin_name": "megaplugin", "dependencies": []}]},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["results"][0]
    assert item["error"]["code"] == "invalid_manifest"
    assert "同时提供" in item["error"]["message"]


def test_batch_missing_root_version_in_registry_is_item_error(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={
            "manifests": [
                {"plugin_name": "megaplugin", "plugin_version": "9.9.9", "dependencies": []}
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["results"][0]
    assert item["error"]["code"] == "invalid_manifest"
    assert "不存在于注册表" in item["error"]["message"]


def test_batch_non_object_item_is_item_error(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": ["not-an-object", 42, None, FEASIBLE_MANIFEST]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] == 3
    assert body["feasible"] == 1
    for item in body["results"][:3]:
        assert item["error"]["code"] == "invalid_manifest"
    assert body["results"][3]["status"] == "feasible"


def test_batch_solver_exception_isolated_and_counted(client, monkeypatch):
    from app import main as main_module

    orig_solve = main_module.Solver.solve

    def patched_solve(self, root_dependencies, root_name=None, root_version=None):
        if any(name == "boom-pkg" for name, _ in root_dependencies):
            raise RuntimeError("simulated solver failure")
        return orig_solve(self, root_dependencies, root_name, root_version)

    monkeypatch.setattr(main_module.Solver, "solve", patched_solve)

    resp = client.post(
        "/api/v1/audits/batch",
        json={
            "manifests": [
                {"dependencies": [{"name": "boom-pkg", "range": "^1.0.0"}]},
                FEASIBLE_MANIFEST,
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["error"] == 1
    assert body["feasible"] == 1

    broken, ok = body["results"]
    assert broken["index"] == 0
    assert broken["error"]["code"] == "solver_error"
    assert "simulated solver failure" in broken["error"]["message"]
    assert ok["index"] == 1
    assert ok["status"] == "feasible"
    # 异常项不得落库
    assert client.get("/api/v1/audits?limit=100").json()["total"] == 1


def test_batch_inconclusive_counted(client, monkeypatch):
    from app import main as main_module
    from app.solver import SolveResult

    orig_solve = main_module.Solver.solve

    def patched_solve(self, root_dependencies, root_name=None, root_version=None):
        if any(name == "force-inconclusive" for name, _ in root_dependencies):
            return SolveResult(
                status="inconclusive",
                node_count=99,
                explanation="budget exhausted (test)",
            )
        return orig_solve(self, root_dependencies, root_name, root_version)

    monkeypatch.setattr(main_module.Solver, "solve", patched_solve)

    resp = client.post(
        "/api/v1/audits/batch",
        json={
            "manifests": [
                {"dependencies": [{"name": "force-inconclusive", "range": "^1.0.0"}]},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inconclusive"] == 1
    assert body["total"] == 1
    item = body["results"][0]
    assert item["status"] == "inconclusive"
    assert item["audit_id"]


def test_batch_empty_list_422(client):
    resp = client.post("/api/v1/audits/batch", json={"manifests": []})
    assert resp.status_code == 422


def test_batch_too_many_items_422(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": [FEASIBLE_MANIFEST] * 21},
    )
    assert resp.status_code == 422
    # 超限时不执行任何一项
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_batch_exactly_20_items_accepted(client):
    resp = client.post(
        "/api/v1/audits/batch",
        json={"manifests": [FEASIBLE_MANIFEST] * 20},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 20
    assert body["feasible"] == 20
    assert len(body["results"]) == 20


def test_batch_envelope_errors_422(client):
    # 缺少 manifests 字段
    assert client.post("/api/v1/audits/batch", json={}).status_code == 422
    # manifests 不是数组
    assert client.post(
        "/api/v1/audits/batch", json={"manifests": FEASIBLE_MANIFEST}
    ).status_code == 422


def test_single_endpoint_behavior_unchanged(client):
    # 单条接口仍然 201
    resp = client.post("/api/v1/audits", json=FEASIBLE_MANIFEST)
    assert resp.status_code == 201
    assert resp.json()["status"] == "feasible"

    # 单条接口对非法清单仍然直接 422（不降级为逐项错误）
    resp = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "x", "range": "not-valid"}]},
    )
    assert resp.status_code == 422
