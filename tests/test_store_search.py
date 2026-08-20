"""調査結果の検索.

「どの条件で調べた結果か」を後から辿れることが目的。
"""

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.store import RunStore
from biomni_hypo.schemas import (
    Evidence,
    Hypothesis,
    ResourceKind,
    RunConfig,
    RunResult,
    VerificationSummary,
)


def _run(
    run_id: str,
    *,
    question: str = "TNBC の PARP 阻害剤耐性は？",
    provider: str = "ollama",
    model: str = "qwen3:14b",
    mode: str = "hypothesis",
    organism: str = "ヒト",
    context: str = "トリプルネガティブ乳がん",
    focus: list[str] | None = None,
    answer: str = "FGFR2 が候補です。",
    days_ago: int = 0,
    status: str = "succeeded",
) -> RunResult:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    return RunResult(
        id=run_id,
        question=question,
        status=status,
        started_at=started,
        question_spec={
            "mode": mode,
            "organism": organism,
            "context": context,
            "focus": focus or ["BRCA1"],
            "background": "",
        },
        config=RunConfig(provider=provider, model=model),
        answer=answer,
        hypotheses=[
            Hypothesis(
                id="h1",
                statement="FGFR2 の発現上昇が耐性に寄与する",
                evidence=[
                    Evidence(eid="E1", kind=ResourceKind.LITERATURE, identifier="PMID:17529967")
                ],
            )
        ],
        verification=VerificationSummary(verified=3, failed=1),
    )


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.sqlite3")
    s.save(_run("r1"))
    s.save(_run("r2", question="乳がんの GWAS 座位を調べたい", provider="anthropic",
                model="claude-opus-5", organism="ヒト", answer="rs2981582 が有力です。"))
    s.save(_run("r3", question="マウスの肝再生の機序", organism="マウス",
                context="肝臓", mode="evidence_check", answer="Wnt シグナルが関わります。",
                days_ago=10))
    return s


def test_conditions_are_stored_as_columns(store):
    (row,) = [r for r in store.search()["runs"] if r["id"] == "r1"]
    assert row["provider"] == "ollama"
    assert row["model"] == "qwen3:14b"
    assert row["mode"] == "hypothesis"
    assert row["organism"] == "ヒト"
    assert row["context"] == "トリプルネガティブ乳がん"
    assert row["focus"] == "BRCA1"


def test_results_are_summarised(store):
    (row,) = [r for r in store.search()["runs"] if r["id"] == "r1"]
    assert row["answer"].startswith("FGFR2")
    assert row["hypothesis_count"] == 1
    assert row["evidence_verified"] == 3
    assert row["evidence_failed"] == 1
    assert row["status"] == "succeeded"


def test_newest_first(store):
    assert [r["id"] for r in store.search()["runs"]][:2] == ["r1", "r2"] or \
           [r["id"] for r in store.search()["runs"]][:2] == ["r2", "r1"]
    assert store.search()["runs"][-1]["id"] == "r3"   # 10 日前


def test_free_text_search_covers_the_question(store):
    assert [r["id"] for r in store.search(query="GWAS")["runs"]] == ["r2"]


def test_free_text_search_covers_the_answer(store):
    assert [r["id"] for r in store.search(query="Wnt")["runs"]] == ["r3"]


def test_free_text_search_covers_hypotheses_and_citations(store):
    assert len(store.search(query="FGFR2")["runs"]) == 3   # 仮説文に含まれる
    assert len(store.search(query="17529967")["runs"]) == 3  # 引用識別子


def test_multiple_terms_are_anded(store):
    assert [r["id"] for r in store.search(query="マウス 肝臓")["runs"]] == ["r3"]
    assert store.search(query="マウス GWAS")["runs"] == []


def test_search_is_case_insensitive(store):
    assert len(store.search(query="pmid")["runs"]) == 3


def test_filter_by_provider(store):
    assert [r["id"] for r in store.search(filters={"provider": "anthropic"})["runs"]] == ["r2"]


def test_filter_by_organism_and_mode(store):
    assert [r["id"] for r in store.search(filters={"organism": "マウス"})["runs"]] == ["r3"]
    assert [r["id"] for r in store.search(filters={"mode": "evidence_check"})["runs"]] == ["r3"]


def test_filters_combine_with_query(store):
    got = store.search(query="FGFR2", filters={"provider": "ollama"})["runs"]
    assert {r["id"] for r in got} == {"r1", "r3"}


def test_unknown_filter_is_ignored(store):
    assert len(store.search(filters={"nonsense": "x"})["runs"]) == 3


def test_date_range(store):
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert {r["id"] for r in store.search(since=recent)["runs"]} == {"r1", "r2"}


def test_total_and_paging(store):
    page = store.search(limit=2)
    assert page["total"] == 3
    assert len(page["runs"]) == 2
    assert len(store.search(limit=2, offset=2)["runs"]) == 1


def test_facets_list_available_filter_values(store):
    facets = store.search()["facets"]
    assert set(facets["provider"]) == {"ollama", "anthropic"}
    assert set(facets["organism"]) == {"ヒト", "マウス"}
    assert set(facets["mode"]) == {"hypothesis", "evidence_check"}


def test_get_returns_the_full_run(store):
    run = store.get("r1")
    assert run.hypotheses[0].statement.startswith("FGFR2")
    assert store.get("nope") is None


def test_delete(store):
    assert store.delete("r1") is True
    assert store.get("r1") is None
    assert store.delete("r1") is False
    assert store.search()["total"] == 2


def test_save_twice_keeps_created_at(store):
    before = [r for r in store.search()["runs"] if r["id"] == "r1"][0]["created_at"]
    run = store.get("r1")
    run.status = "failed"
    store.save(run)
    after = [r for r in store.search()["runs"] if r["id"] == "r1"][0]
    assert after["created_at"] == before
    assert after["status"] == "failed"


def test_migrates_an_old_database(tmp_path):
    """列を追加する前の DB を開いても壊れないこと。"""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, question TEXT, status TEXT,"
        " created_at TEXT, updated_at TEXT, payload TEXT);"
    )
    run = _run("old1")
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
        ("old1", run.question, "succeeded", run.started_at.isoformat(),
         run.started_at.isoformat(), run.model_dump_json()),
    )
    conn.commit()
    conn.close()

    store = RunStore(path)
    (row,) = store.search()["runs"]
    assert row["id"] == "old1"
    assert row["provider"] == "ollama"        # payload から埋め直された
    assert row["hypothesis_count"] == 1
    assert store.search(query="FGFR2")["total"] == 1
