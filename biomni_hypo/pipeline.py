"""ラン 1 本を通す共通エントリポイント.

ノートブック（notebooks/04_end_to_end.ipynb）と Web ワーカー
（backend/app/worker.py）は、どちらもこの関数を呼ぶ。
処理をここに集約しておけば、ノートブックで検証したものがそのまま本番になる。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from biomni_hypo.agent_factory import AgentBundle, build_agent
from biomni_hypo.config import Settings
from biomni_hypo.extractor import HypothesisExtractor
from biomni_hypo.policy import ResourcePolicy
from biomni_hypo.question import ResearchQuestion, coerce_question
from biomni_hypo.schemas import (
    Resource,
    ResourceKind,
    RunResult,
    Step,
    StepKind,
)
from biomni_hypo.tracing import PARSE_ERROR_HINT, TracingRunner
from biomni_hypo.verifier import EvidenceVerifier

log = logging.getLogger(__name__)

EventFn = Callable[[str, dict[str, Any]], None]


def run_hypothesis(
    question: ResearchQuestion | str,
    *,
    settings: Settings | None = None,
    policy: ResourcePolicy | None = None,
    bundle: AgentBundle | None = None,
    extractor: HypothesisExtractor | None = None,
    verifier: EvidenceVerifier | None = None,
    on_event: EventFn | None = None,
    run_id: str | None = None,
) -> RunResult:
    """研究課題 -> 検証済みの仮説と根拠.

    Args:
        question: ResearchQuestion（推奨）か、自由記述の文字列。
        bundle: 既存の A1 を使い回す場合に渡す（構築は重い）。
        on_event: SSE 配信用のコールバック。ノートブックでは省略可。

    Raises:
        ValueError: 入力に error レベルの Hint がある場合（実行前に止める）。
    """
    settings = settings or (bundle.settings if bundle else Settings())
    policy = policy or (bundle.policy if bundle else ResourcePolicy.load(settings.policy_path))
    run_id = run_id or f"r_{uuid.uuid4().hex[:12]}"
    t0 = time.monotonic()

    spec = coerce_question(question)
    blocking = spec.blocking_hints
    if blocking:
        raise ValueError("入力に問題があります: " + " / ".join(h.message for h in blocking))
    if spec.max_hypotheses != settings.max_hypotheses:
        settings = settings.model_copy(update={"max_hypotheses": spec.max_hypotheses})
    prompt = spec.to_prompt(settings.prompt_language)

    def emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, payload)

    if bundle is None:
        emit("phase", {"phase": "building_agent"})
        bundle = build_agent(settings, policy)

    result = RunResult(
        id=run_id,
        question=spec.summary,
        question_spec=spec.as_spec(),
        prompt=prompt,
        config=settings.to_run_config(
            policy_version=policy.version, biomni_version=bundle.biomni_version
        ),
    )
    hints = [h.as_dict() for h in spec.hints(commercial_mode=settings.commercial_mode)]
    if hints:
        result.extra["input_hints"] = hints
        emit("input_hints", {"hints": hints})

    # --- 1. 探索フェーズ ------------------------------------------------------
    emit("phase", {"phase": "exploring"})
    runner = TracingRunner(bundle, run_id=run_id)
    try:
        for _step in runner.iter_steps(prompt, on_event=on_event):
            pass
    except Exception as exc:  # noqa: BLE001 - 途中結果を残すことを優先する
        log.exception("探索フェーズで例外")
        result.error = f"{type(exc).__name__}: {exc}"
    trace = runner.result()
    result.steps = trace.steps
    result.solution_text = trace.solution_text
    result.resources_considered = trace.resources_considered
    result.plan = trace.plan
    result.plan_revisions = trace.plan_revisions
    if not result.plan:
        # biomni は計画を立てるよう指示しているので、無いのは異常寄り
        # （指示追従性が低いモデル）。分かるようにしておく
        result.extra["plan_missing"] = True
    if trace.stopped_reason:
        result.extra["stopped_reason"] = trace.stopped_reason
    if trace.hallucinated_observations:
        # AC-1 違反。stop シーケンスが効いていない状態でのランは信用できない。
        result.extra["hallucinated_observations"] = trace.hallucinated_observations
        log.error(
            "LLM が observation を自己生成しました（%s 回）。"
            "docs/design/04 §4.1 の LLM 差し替えが効いているか確認してください。",
            trace.hallucinated_observations,
        )
    if trace.parsing_errors:
        # モデルが <execute>/<solution> を出せていない。探索そのものが進んでいない
        # 可能性が高いので、結果と一緒に必ず出す（docs/design/16 §16.2）。
        result.extra["parsing_errors"] = trace.parsing_errors
        result.extra["parsing_error_hint"] = PARSE_ERROR_HINT

    # --- 2. 抽出フェーズ ------------------------------------------------------
    emit("phase", {"phase": "extracting"})
    extractor = extractor or HypothesisExtractor(settings)
    # 抽出は「元の問い」を見せる。組み立て済みプロンプトより人の意図に近い
    extraction = extractor.extract(spec.summary, trace.steps, trace.solution_text)
    if extraction.unknown_eids:
        result.extra["unknown_eids"] = sorted(set(extraction.unknown_eids))
    if extraction.parse_error:
        result.extra["extraction_error"] = extraction.parse_error

    # --- 3. 検証フェーズ ------------------------------------------------------
    emit("phase", {"phase": "verifying"})
    verifier = verifier or EvidenceVerifier(offline=settings.offline_mode)
    # 回答の根拠も同じ基準で検証する（仮説と扱いを変えない）
    result.answer = extraction.answer
    result.answer_evidence = verifier.verify_evidence_list(extraction.answer_evidence, trace.steps)
    # 論点の根拠も同じ検証を通す。論点だけ検証を免除すると、
    # 「もっともらしい筋道」が無検証で通ってしまう
    result.answer_reasoning = []
    for point in extraction.answer_reasoning:
        point.evidence = verifier.verify_evidence_list(point.evidence, trace.steps)
        result.answer_reasoning.append(point)
    result.answer_uncertainties = list(extraction.answer_uncertainties)
    supported, unsupported, report = verifier.verify_run(extraction.hypotheses, trace.steps)
    result.hypotheses = supported
    result.unsupported_ideas = unsupported
    result.failed_citations = report.failed
    result.verification = report.summary
    emit("verification", report.summary.model_dump(mode="json"))

    # --- 4. 使用リソースの集計 ------------------------------------------------
    result.resources_used = collect_resources(trace.steps, policy)

    if not result.answer and trace.solution_text:
        # 抽出が失敗しても、エージェントの結論だけは見せる
        result.answer = trace.solution_text
        result.extra["answer_is_unstructured"] = True

    if not result.answer_reasoning and result.answer:
        # 結論はあるのに論点が無い＝biomni 既定の「短い答え」に戻っている状態
        # （docs/design/18 §18.1）。黙って結論だけ出さず、必ず旗を立てる。
        #
        # 「モデルが返さなかった」のか「こちらが捨てた」のかを必ず言い分ける。
        # 一括りに「抽出できませんでした」と出していたため、原因が分からず
        # モデルを替えても直らない、という状態が続いた（docs/design/30）。
        result.extra["reasoning_missing"] = True
        if extraction.parse_error:
            reason = f"抽出応答を読めませんでした（{extraction.parse_error}）"
        elif extraction.reasoning_seen == 0:
            reason = "抽出モデルが reasoning を返しませんでした"
        else:
            reason = (
                f"reasoning は {extraction.reasoning_seen} 件ありましたが、"
                f"形が想定と違うため使えませんでした"
            )
        result.extra["reasoning_missing_reason"] = reason
        log.warning("回答は得られましたが論点がありません: %s", reason)

    result.status = "failed" if (result.error and not result.steps) else "succeeded"
    result.finished_at = datetime.now(UTC)
    result.extra["duration_sec"] = round(time.monotonic() - t0, 1)
    emit("done", {"status": result.status, "duration_sec": result.extra["duration_sec"]})
    return result


def collect_resources(steps: Iterable[Step], policy: ResourcePolicy) -> list[Resource]:
    """トレースが実際に触れたリソースを、ライセンス情報付きで集計する（3 段階の B）。

    レポートの「使用データとライセンス」セクションの元になる（docs/design/05 §5.3）。
    """
    datasets: dict[str, list[int]] = {}
    tools: dict[str, tuple[str, list[int]]] = {}
    user_files: dict[str, list[int]] = {}

    for s in steps:
        if s.kind != StepKind.EXECUTE:
            continue
        for name in s.datasets:
            datasets.setdefault(name, []).append(s.idx)
        for name in s.user_files:
            user_files.setdefault(name, []).append(s.idx)
        for t in s.tools:
            entry = tools.setdefault(t.name, (t.module, []))
            entry[1].append(s.idx)

    out: list[Resource] = [policy.describe_dataset(n, idxs) for n, idxs in sorted(datasets.items())]

    for name, (module, idxs) in sorted(tools.items()):
        d = policy.check_tool(name)
        out.append(
            Resource(
                kind=ResourceKind.TOOL,
                name=name,
                identifier=module,
                license=d.license,
                attribution=module,
                commercial_ok=d.allowed,
                review_required=d.review_required,
                step_idxs=idxs,
            )
        )

    for name, idxs in sorted(user_files.items()):
        out.append(
            Resource(
                kind=ResourceKind.USER_FILE,
                name=name,
                license="user-provided",
                commercial_ok=True,
                step_idxs=idxs,
            )
        )
    return out


def summarize(result: RunResult) -> str:
    """ノートブックで 1 行確認するための要約。"""
    v = result.verification
    return (
        f"[{result.status}] {len(result.steps)} ステップ / "
        f"仮説 {len(result.hypotheses)} 件（未裏付け {len(result.unsupported_ideas)} 件）/ "
        f"根拠 検証済 {v.verified} · 検証不能 {v.not_applicable} · 失敗 {v.failed} "
        f"（検証率 {v.rate:.0%}）"
    )
