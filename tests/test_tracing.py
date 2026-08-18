from biomni_hypo.config import Settings
from biomni_hypo.fixtures import (
    TRACE_MESSAGES,
    TRACE_MESSAGES_HALLUCINATED,
    TRACE_MESSAGES_POLICY,
    FakeA1Module,
    fake_bundle,
)
from biomni_hypo.schemas import StepKind
from biomni_hypo.tracing import TracingRunner


def _run(messages, settings=None):
    bundle = fake_bundle(messages, settings=settings)
    runner = TracingRunner(bundle, run_id="t_test", guard_module=FakeA1Module())
    return runner.run("テスト質問"), runner


def test_steps_are_classified():
    result, _ = _run(TRACE_MESSAGES)
    kinds = [s.kind for s in result.steps]
    assert kinds.count(StepKind.EXECUTE) == 2
    assert kinds.count(StepKind.OBSERVATION) == 2
    assert kinds[-1] == StepKind.SOLUTION
    assert result.solution_text


def test_input_question_is_not_a_step():
    result, _ = _run(TRACE_MESSAGES)
    assert all("テスト質問" not in s.text for s in result.steps)


def test_execute_step_captures_tools_and_datasets():
    result, _ = _run(TRACE_MESSAGES)
    exec_steps = [s for s in result.steps if s.kind == StepKind.EXECUTE]
    assert [t.name for t in exec_steps[0].tools] == ["query_gwas_catalog"]
    assert exec_steps[0].datasets == ["gwas_catalog.pkl"]
    assert exec_steps[1].datasets == ["DepMap_CRISPRGeneEffect.csv"]


def test_only_known_data_lake_files_count_as_datasets():
    """コードに出てくる任意のファイル名を勝手にデータセット扱いしない。"""
    messages = ["<execute>\npd.read_csv('random_scratch.csv')\n</execute>", "<solution>x</solution>"]
    result, _ = _run(messages)
    assert [s.datasets for s in result.steps if s.kind == StepKind.EXECUTE] == [[]]


def test_observation_citations_are_extracted():
    result, _ = _run(TRACE_MESSAGES)
    obs = [s for s in result.steps if s.kind == StepKind.OBSERVATION][0]
    assert {c.identifier for c in obs.citations} >= {"PMID:17529967", "rs2981582"}
    assert all(c.step_idx == obs.idx for c in obs.citations)


def test_think_preamble_is_split_from_execute():
    result, _ = _run(TRACE_MESSAGES)
    assert result.steps[0].kind == StepKind.THINK
    assert result.steps[1].kind == StepKind.EXECUTE


def test_hallucinated_observation_is_detected():
    """stop シーケンスが効いていないランを検知する（受け入れ基準 AC-1）。"""
    result, _ = _run(TRACE_MESSAGES_HALLUCINATED)
    assert result.hallucinated_observations == 1


def test_clean_run_reports_no_hallucination():
    result, _ = _run(TRACE_MESSAGES)
    assert result.hallucinated_observations == 0


def test_policy_blocked_observation_gets_its_own_kind():
    result, _ = _run(TRACE_MESSAGES_POLICY)
    assert any(s.kind == StepKind.POLICY_BLOCKED for s in result.steps)


def test_guard_prevents_denied_code_from_running():
    module = FakeA1Module()
    bundle = fake_bundle(TRACE_MESSAGES_POLICY)
    # FakeGraph は実行しないので、ガード単体の振る舞いを直接確認する
    from biomni_hypo.guard import policy_guard

    with policy_guard(bundle.policy, module) as guard:
        out = module.run_python_repl("from biomni.tool.database import query_kegg")
        assert out.startswith("POLICY BLOCKED")
        assert guard.blocked
    assert module.executed == []
    # コンテキストを抜けたら元に戻っていること
    assert module.run_python_repl("print(1)") == "OK"


def test_max_steps_stops_the_run():
    settings = Settings(max_steps=2)
    result, _ = _run(TRACE_MESSAGES, settings=settings)
    assert len(result.steps) <= 3  # 1 メッセージが複数ステップに分かれる分の余地
    assert "max_steps" in result.stopped_reason


def test_run_id_is_used_as_thread_id():
    """A1.go() は thread_id を 42 に固定する。ラン間で状態が混ざらないようにする。"""
    bundle = fake_bundle(TRACE_MESSAGES)
    runner = TracingRunner(bundle, run_id="r_unique", guard_module=FakeA1Module())
    runner.run("q")
    assert bundle.agent.app.calls[0]["configurable"]["thread_id"] == "r_unique"


def test_on_event_callback_receives_steps():
    events = []
    bundle = fake_bundle(TRACE_MESSAGES)
    runner = TracingRunner(bundle, guard_module=FakeA1Module())
    for _ in runner.iter_steps("q", on_event=lambda k, p: events.append((k, p))):
        pass
    kinds = [k for k, _ in events]
    assert kinds.count("step") == len(runner.steps)
    assert kinds[-1] == "trace_done"
