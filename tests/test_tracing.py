import pytest

from biomni_hypo.config import Settings
from biomni_hypo.fixtures import (
    TRACE_MESSAGES,
    TRACE_MESSAGES_HALLUCINATED,
    TRACE_MESSAGES_PARSE_GIVEUP,
    TRACE_MESSAGES_PARSE_RETRY,
    TRACE_MESSAGES_PLANNED,
    TRACE_MESSAGES_POLICY,
    FakeA1Module,
    fake_bundle,
)
from biomni_hypo.schemas import StepKind
from biomni_hypo.tracing import TracingRunner, parse_plan


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


# ------------------------------------------- タグ無し応答（biomni の差し戻し）
# 実測: 画面に「0 think Each response must include thinking process…」とだけ出て、
# 何が起きたのか分からなかった。フレームワークの差し戻しは think ではない。


def test_parse_retry_is_not_classified_as_think():
    result, _ = _run(TRACE_MESSAGES_PARSE_RETRY)
    kinds = [s.kind for s in result.steps]
    assert StepKind.PARSING_ERROR in kinds
    parse_steps = [s for s in result.steps if s.kind == StepKind.PARSING_ERROR]
    assert len(parse_steps) == 1
    # 英文の叱責をそのまま出さず、日本語で原因と対処を出す
    assert "差し戻し" in parse_steps[0].text
    assert "num_ctx" in parse_steps[0].text
    # 原文も error に残す（何が起きたか追えるように）
    assert "no tags in the current response" in parse_steps[0].error
    assert result.parsing_errors == 1


def test_run_recovers_after_a_retry():
    """差し戻しのあとタグ付きで返せば、ランはそのまま続く。"""
    result, _ = _run(TRACE_MESSAGES_PARSE_RETRY)
    kinds = [s.kind for s in result.steps]
    assert StepKind.EXECUTE in kinds
    assert kinds[-1] == StepKind.SOLUTION
    assert result.solution_text


def test_repeated_parse_errors_set_a_stopped_reason():
    result, _ = _run(TRACE_MESSAGES_PARSE_GIVEUP)
    assert result.parsing_errors == 3  # 差し戻し 2 回 + 打ち切り 1 回
    assert "打ち切り" in result.stopped_reason
    assert "num_ctx" in result.stopped_reason
    assert not result.solution_text


def test_clean_run_reports_no_parsing_errors():
    result, _ = _run(TRACE_MESSAGES)
    assert result.parsing_errors == 0
    assert all(s.kind != StepKind.PARSING_ERROR for s in result.steps)


# --------------------------------------------------------------- 解析の計画
# biomni は "Given a task, make a plan first." と指示し、
# "Always show the updated plan after each step" と毎ターン再掲させる。
# 素通しすると think に埋もれるので、独立した種別として拾う（docs/design/19）。


def test_plan_is_its_own_step_kind():
    result, _ = _run(TRACE_MESSAGES_PLANNED)
    plans = [s for s in result.steps if s.kind == StepKind.PLAN]
    assert plans, "計画が PLAN として拾えていない"
    assert all(s.plan for s in plans), "PLAN ステップに中身が入っていない"


def test_plan_checkboxes_are_parsed():
    items = parse_plan(
        "1. [ ] First step\n2. [✓] Second step (completed)\n3. [✗] Third (failed because X)"
    )
    assert [i.state for i in items] == ["todo", "done", "failed"]
    assert items[1].text == "Second step"
    assert "failed because X" in items[2].note


@pytest.mark.parametrize("mark", ["✓", "✔", "x", "X", "☑"])
def test_done_marks_vary_by_model(mark):
    items = parse_plan(f"1. [{mark}] done step\n2. [ ] next step")
    assert items[0].state == "done"


@pytest.mark.parametrize("mark", ["✗", "×", "-", "!"])
def test_failure_marks_vary_by_model(mark):
    items = parse_plan(f"1. [{mark}] broken step\n2. [ ] next step")
    assert items[0].state == "failed"


def test_a_single_checkbox_line_is_not_a_plan():
    """1 行だけの箇条書きを計画と呼ばない。"""
    assert len(parse_plan("1. [ ] 何か")) == 1  # パースはできるが
    result, runner = _run(["1. [ ] 何か\n<execute>\nprint(1)\n</execute>", "<solution>x</solution>"])
    assert not [s for s in result.steps if s.kind == StepKind.PLAN]


def test_the_latest_plan_wins():
    """毎ターン再掲されるので、最後の状態が残ること。"""
    result, _ = _run(TRACE_MESSAGES_PLANNED)
    assert [i.state for i in result.plan] == ["done", "failed", "done"]
    assert result.plan[1].note, "失敗理由が落ちている"


def test_the_final_plan_update_before_a_solution_is_kept():
    """最終ターンの計画を取りこぼさない。

    ここを飛ばすと「最後に何が終わって何が失敗したか」が残らない。
    """
    result, _ = _run(TRACE_MESSAGES_PLANNED)
    kinds = [s.kind for s in result.steps]
    assert kinds[-2] == StepKind.PLAN and kinds[-1] == StepKind.SOLUTION


def test_repeating_the_same_plan_does_not_add_a_step():
    """進捗が変わらない再掲でステップを水増ししない。"""
    plan = "1. [ ] a\n2. [ ] b\n"
    result, _ = _run([
        plan + "<execute>\nprint(1)\n</execute>",
        "<observation>ok</observation>",
        plan + "<execute>\nprint(2)\n</execute>",
        "<solution>done</solution>",
    ])
    assert len([s for s in result.steps if s.kind == StepKind.PLAN]) == 1


def test_reordering_the_plan_counts_as_a_revision():
    result, _ = _run([
        "1. [ ] a\n2. [ ] b\n<execute>\nprint(1)\n</execute>",
        "<observation>ok</observation>",
        "1. [ ] a\n2. [ ] c\n3. [ ] d\n<execute>\nprint(2)\n</execute>",
        "<solution>done</solution>",
    ])
    assert result.plan_revisions == 1


def test_ticking_a_box_is_progress_not_a_revision():
    result, _ = _run([
        "1. [ ] a\n2. [ ] b\n<execute>\nprint(1)\n</execute>",
        "<observation>ok</observation>",
        "1. [✓] a\n2. [ ] b\n<execute>\nprint(2)\n</execute>",
        "<solution>done</solution>",
    ])
    assert result.plan_revisions == 0
    assert result.plan[0].state == "done"


def test_prose_around_the_plan_is_kept_as_think():
    result, _ = _run(TRACE_MESSAGES_PLANNED)
    thinks = [s for s in result.steps if s.kind == StepKind.THINK]
    assert any("計画を立てます" in s.text for s in thinks)
    assert all("[ ]" not in s.text for s in thinks), "計画の行が think に混ざっている"


def test_a_run_without_a_plan_is_still_fine():
    result, _ = _run(TRACE_MESSAGES)
    assert not result.plan
    assert not [s for s in result.steps if s.kind == StepKind.PLAN]
