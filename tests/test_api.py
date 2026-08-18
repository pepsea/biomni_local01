import pytest
from fastapi.testclient import TestClient

import backend.app.main as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.app.store import RunStore

    monkeypatch.setattr(main, "STORE", RunStore(tmp_path / "runs.sqlite3"))
    main._running.clear()
    main._subscribers.clear()
    main._seq.clear()
    return TestClient(main.app)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["api"] == "ok"
    assert body["commercial_mode"] is True
    assert body["policy_version"] >= 1


def test_policy_endpoint_exposes_the_enforced_lists(client):
    body = client.get("/api/policy").json()
    assert body["mode"] == "commercial_only"
    assert "query_kegg" in body["denied_tools"]
    assert "gwas_catalog.pkl" in body["allowed_datasets"]
    assert "qwen3:14b" in body["allowed_models"]


def test_models_endpoint_lists_allowed_models_even_when_not_pulled(client):
    body = client.get("/api/models").json()
    names = {m["name"] for m in body["models"]}
    assert "qwen3:14b" in names
    assert all(m["allowed"] for m in body["models"] if m["name"] == "qwen3:14b")


def test_run_with_denied_model_is_rejected(client):
    r = client.post("/api/runs", json={"question": "q", "model": "llama3.1:8b"})
    assert r.status_code == 422
    assert "policy_violation" in r.text


def test_run_requires_a_question(client):
    assert client.post("/api/runs", json={"question": ""}).status_code == 422


def test_unknown_run_returns_404(client):
    assert client.get("/api/runs/r_nope").status_code == 404
    assert client.get("/api/runs/r_nope/events").status_code == 404
    assert client.get("/api/runs/r_nope/report").status_code == 404


def test_stored_run_is_served_with_report(client):
    from biomni_hypo.fixtures import sample_steps
    from biomni_hypo.schemas import RunResult

    run = RunResult(id="r_x", question="テスト", status="succeeded", steps=sample_steps())
    main.STORE.save(run)

    body = client.get("/api/runs/r_x").json()
    assert body["question"] == "テスト"
    assert len(body["steps"]) == len(run.steps)

    md = client.get("/api/runs/r_x/report").text
    assert "# 仮説構築レポート" in md
    assert "## 実行トレース" in md

    listed = client.get("/api/runs").json()["runs"]
    assert [r["id"] for r in listed] == ["r_x"]


def test_events_replay_from_store(client):
    from biomni_hypo.schemas import RunResult

    main.STORE.save(RunResult(id="r_e", question="q"))
    main.STORE.append_event("r_e", 1, "status", {"status": "running"})
    main.STORE.append_event("r_e", 2, "done", {"status": "succeeded"})

    with client.stream("GET", "/api/runs/r_e/events") as r:
        text = "".join(chunk for chunk in r.iter_text())
    assert "event: status" in text
    assert "event: done" in text
    assert "id: 2" in text
