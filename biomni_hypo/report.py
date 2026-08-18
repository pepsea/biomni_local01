"""Markdown レポート生成（docs/design/03-evidence-model.md §3.6）.

必ず含めるもの:
  - 再現に必要な設定（モデル・モード・biomni バージョン・ポリシー版）
  - 仮説と、その根拠（識別子・抜粋・由来ステップ）
  - 実行トレース全文（コードと出力）
  - 引用の検証状況（失敗したものも隠さない）
  - 使用データとライセンス（CC BY 系の帰属義務をここで機械的に満たす）
"""

from __future__ import annotations

from biomni_hypo.schemas import (
    Hypothesis,
    ResourceKind,
    RunResult,
    StepKind,
    VerificationStatus,
)

_STATUS_MARK = {
    VerificationStatus.VERIFIED: "✓ 検証済",
    VerificationStatus.NOT_APPLICABLE: "− 検証不能",
    VerificationStatus.UNVERIFIED: "? 未検証",
    VerificationStatus.FAILED: "✕ 検証失敗",
}

_KIND_LABEL = {
    ResourceKind.DATASET: "データ",
    ResourceKind.USER_FILE: "自前データ",
    ResourceKind.TOOL: "ツール",
    ResourceKind.LIBRARY: "ライブラリ",
    ResourceKind.LITERATURE: "文献",
    ResourceKind.DB_RECORD: "DB レコード",
    ResourceKind.COMPUTATION: "計算",
    ResourceKind.KNOW_HOW: "ノウハウ",
}


def to_markdown(result: RunResult, *, include_trace: bool = True) -> str:
    parts = [
        _header(result),
        _hypotheses_section(result),
        _unsupported_section(result),
        _verification_section(result),
        _licenses_section(result),
    ]
    if include_trace:
        parts.append(_trace_section(result))
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


def _header(r: RunResult) -> str:
    c = r.config
    review = [x for x in r.resources_used if x.review_required]
    lines = [
        "# 仮説構築レポート",
        "",
        f"**研究課題**: {r.question}",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| ラン ID | `{r.id}` |",
        f"| 状態 | {r.status} |",
        f"| モデル | {c.model}（Ollama, temperature={c.temperature}, num_ctx={c.num_ctx}） |",
        f"| モード | 商用限定={c.commercial_mode} / オフライン={c.offline_mode} |",
        f"| biomni | {c.biomni_version or '-'} |",
        f"| ポリシー版 | {c.policy_version} |",
        f"| 実行 | {r.started_at:%Y-%m-%d %H:%M:%S} UTC · {r.extra.get('duration_sec', '-')} 秒 · {len(r.steps)} ステップ |",
    ]
    if r.extra.get("stopped_reason"):
        lines.append(f"| 打ち切り | {r.extra['stopped_reason']} |")
    if r.extra.get("hallucinated_observations"):
        lines += [
            "",
            "> ⚠️ **このランの結果は信用できません。** LLM が実行結果（`<observation>`）を"
            f"自己生成しました（{r.extra['hallucinated_observations']} 回）。"
            "stop シーケンスが効いていません（docs/design/04 §4.1）。",
        ]
    if review:
        names = ", ".join(x.name for x in review)
        lines += ["", f"> ⚠️ ライセンスの確認が必要なリソースを使用しています: {names}"]
    return "\n".join(lines)


def _hypotheses_section(r: RunResult) -> str:
    if not r.hypotheses:
        return "## 仮説\n\n裏付けのある仮説は得られませんでした。"
    out = ["## 仮説"]
    for i, h in enumerate(r.hypotheses, start=1):
        out.append(_hypothesis_block(i, h))
    return "\n\n".join(out)


def _hypothesis_block(i: int, h: Hypothesis) -> str:
    lines = [
        f"### 仮説 {i}. {h.statement}",
        "",
        f"確度: **{h.confidence}** · 新規性: **{h.novelty}** · 根拠 {len(h.evidence)} 件",
        "",
        h.rationale or "",
        "",
        "**根拠**",
        "",
        "| 種別 | 識別子 | 立場 | 検証 | 抜粋 | 由来 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for ev in h.evidence:
        excerpt = ev.excerpt.replace("|", "\\|")[:160]
        ident = f"[{ev.identifier}]({ev.url})" if ev.url else ev.identifier
        lines.append(
            f"| {_KIND_LABEL.get(ev.kind, ev.kind.value)} | {ident} | {ev.stance.value} | "
            f"{_STATUS_MARK.get(ev.verification_status, '?')} | {excerpt} | ステップ {ev.step_idx} |"
        )
    if h.assumptions:
        lines += ["", "**前提**", ""] + [f"- {a}" for a in h.assumptions]
    tp = h.test_plan
    if tp.experiment or tp.readout:
        lines += [
            "",
            "**検証プラン**",
            "",
            f"- 実験系: {tp.experiment}",
            f"- 読み出し: {tp.readout}",
            f"- 対照: {', '.join(tp.controls) or '-'}",
            f"- 実行可能性: {tp.feasibility} / {tp.estimated_effort or '-'}",
        ]
    return "\n".join(lines)


def _unsupported_section(r: RunResult) -> str:
    if not r.unsupported_ideas:
        return ""
    lines = ["## 未裏付けの着想", "", "根拠が紐付かなかったもの。研究の種としてのみ扱うこと。", ""]
    lines += [f"- {h.statement}" for h in r.unsupported_ideas]
    return "\n".join(lines)


def _verification_section(r: RunResult) -> str:
    v = r.verification
    lines = [
        "## 根拠の検証",
        "",
        f"- 検証済: **{v.verified}** / 検証不能: {v.not_applicable} / 未検証: {v.unverified} / 検証失敗: **{v.failed}**",
        f"- 引用検証率: **{v.rate:.0%}**",
        f"- 裏付けのある仮説: {len(r.hypotheses)} / {len(r.hypotheses) + len(r.unsupported_ideas)}",
    ]
    if r.extra.get("unknown_eids"):
        lines.append(
            f"- 存在しない根拠 ID を参照したため破棄: {', '.join(r.extra['unknown_eids'])}"
        )
    if r.failed_citations:
        lines += ["", "### 検証に失敗した引用", "", "| 識別子 | 種別 | 理由 | ステップ |", "| --- | --- | --- | --- |"]
        for f in r.failed_citations:
            lines.append(
                f"| {f.identifier} | {_KIND_LABEL.get(f.kind, f.kind.value)} | {f.reason} | {f.step_idx} |"
            )
    return "\n".join(lines)


def _licenses_section(r: RunResult) -> str:
    if not r.resources_used:
        return ""
    lines = [
        "## 使用データとライセンス",
        "",
        "| リソース | 種別 | ライセンス | 帰属 | 商用 | 使用ステップ |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for res in r.resources_used:
        mark = "⚠️ 要確認" if res.review_required else ("✅" if res.commercial_ok else "❌")
        steps = ", ".join(str(i) for i in res.step_idxs) or "-"
        lines.append(
            f"| {res.name} | {_KIND_LABEL.get(res.kind, res.kind.value)} | {res.license} | "
            f"{res.attribution or '-'} | {mark} | {steps} |"
        )
    return "\n".join(lines)


def _trace_section(r: RunResult) -> str:
    lines = ["## 実行トレース"]
    for s in r.steps:
        if s.kind == StepKind.EXECUTE:
            tools = ", ".join(t.name for t in s.tools) or "-"
            data = ", ".join(s.datasets + s.user_files) or "-"
            lines += [
                "",
                f"### ステップ {s.idx} — 実行 ({s.duration_ms} ms)",
                f"ツール: {tools} / データ: {data}",
                "",
                "```python",
                s.code,
                "```",
            ]
        elif s.kind == StepKind.OBSERVATION:
            lines += ["", f"### ステップ {s.idx} — 観測", "", "```", s.text[:4000], "```"]
        elif s.kind == StepKind.POLICY_BLOCKED:
            lines += ["", f"### ステップ {s.idx} — ポリシーによりブロック", "", "```", s.text, "```"]
        elif s.kind == StepKind.SOLUTION:
            lines += ["", f"### ステップ {s.idx} — 結論", "", s.text]
        else:
            lines += ["", f"### ステップ {s.idx} — 思考", "", s.text[:2000]]
    return "\n".join(lines)
