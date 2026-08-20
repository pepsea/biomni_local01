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
    Stance,
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
        _question_section(result),
        _plan_section(result),
        _answer_section(result),
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
        f"| モデル | {c.model}（{c.provider}{'' if c.provider != 'ollama' else f', num_ctx={c.num_ctx}'}） |",
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


_SPEC_LABELS = {
    "mode": "モード",
    "organism": "生物種",
    "context": "対象",
    "focus": "注目対象",
    "background": "前提",
    "exclude": "除外",
    "dataset_ids": "使用データ",
}


def _question_section(r: RunResult) -> str:
    """何を聞いたか、実際に何を投げたかを残す。

    プロンプトを隠すと、結果を検証できる人がいなくなる。
    """
    spec = r.question_spec or {}
    lines = ["## 入力", "", f"**{r.question}**", ""]

    rows = []
    for key, label in _SPEC_LABELS.items():
        value = spec.get(key)
        if not value:
            continue
        text = ", ".join(value) if isinstance(value, list) else str(value)
        rows.append(f"| {label} | {text} |")
    if rows:
        lines += ["| 項目 | 内容 |", "| --- | --- |", *rows, ""]

    for hint in r.extra.get("input_hints", []):
        mark = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(hint.get("severity"), "・")
        lines.append(f"{mark} {hint.get('message')}")
    if r.extra.get("input_hints"):
        lines.append("")

    if r.prompt:
        lines += ["<details><summary>エージェントに渡したプロンプト</summary>", "", "```", r.prompt, "```", "", "</details>"]
    return "\n".join(lines)


_PLAN_MARK = {"todo": "☐", "done": "✓", "failed": "✗"}


def _plan_section(r: RunResult) -> str:
    """解析の設計。biomni が最初に立てる計画（docs/design/19）。"""
    if not r.plan:
        return ""
    done = sum(1 for i in r.plan if i.state == "done")
    failed = sum(1 for i in r.plan if i.state == "failed")
    lines = ["## 解析の設計", ""]
    for item in r.plan:
        note = f"（{item.note}）" if item.note else ""
        lines.append(f"- {_PLAN_MARK.get(item.state, '☐')} {item.text}{note}")
    tail = f"{done}/{len(r.plan)} 完了"
    if failed:
        tail += f"・{failed} 件失敗"
    if r.plan_revisions:
        tail += f"・計画を {r.plan_revisions} 回立て直し"
    lines += ["", f"（{tail}）", ""]
    return "\n".join(lines)


def _answer_section(r: RunResult) -> str:
    """質問への回答。根拠付きで、いちばん上に出す。"""
    if not r.answer:
        return ""
    lines = ["## 回答", "", r.answer, ""]
    if r.extra.get("answer_is_unstructured"):
        lines.append("> ⚠️ 構造化に失敗したため、エージェントの結論をそのまま載せています。")
        lines.append("")
    if r.answer_evidence:
        lines += ["**この回答の根拠**", "", "| 種別 | 識別子 | 検証 | 由来 |", "| --- | --- | --- | --- |"]
        for ev in r.answer_evidence:
            ident = f"[{ev.identifier}]({ev.url})" if ev.url else ev.identifier
            lines.append(
                f"| {_KIND_LABEL.get(ev.kind, ev.kind.value)} | {ident} | "
                f"{_STATUS_MARK.get(ev.verification_status, '?')} | ステップ {ev.step_idx} |"
            )
    else:
        lines.append("> ⚠️ この回答には検証を通った根拠が紐付いていません。")
    lines += ["", *_reasoning_lines(r)]
    return "\n".join(lines)


_STANCE_LABEL = {Stance.SUPPORTS: "支持", Stance.REFUTES: "反証", Stance.CONTEXT: "判断材料"}
_WEIGHT_LABEL = {"decisive": "決め手", "supporting": "補強", "weak": "弱い"}


def _reasoning_lines(r: RunResult) -> list[str]:
    """結論に至った論点。レポートでも結論だけを載せない（docs/design/18）。"""
    lines: list[str] = []
    if r.answer_reasoning:
        lines += ["### この結論に至った論点", ""]
        # 反証を最後に押しやらない
        ordered = sorted(r.answer_reasoning, key=lambda p: p.stance is not Stance.REFUTES)
        for i, pt in enumerate(ordered, 1):
            lines.append(
                f"{i}. **{pt.point}**  "
                f"（{_STANCE_LABEL.get(pt.stance, pt.stance.value)}・"
                f"{_WEIGHT_LABEL.get(pt.weight, pt.weight)}）"
            )
            if pt.finding:
                lines.append(f"   - 分かったこと: {pt.finding}")
            if pt.evidence:
                refs = ", ".join(
                    f"{e.identifier} {_STATUS_MARK.get(e.verification_status, '?')}"
                    for e in pt.evidence
                )
                lines.append(f"   - 根拠: {refs}")
            else:
                lines.append("   - 根拠: なし（この論点は裏付けられていません）")
        lines.append("")
    elif r.answer:
        lines += [
            "> ⚠️ 論点を抽出できませんでした。抽出モデルが結論だけを返しています。",
            "",
        ]
    if r.answer_uncertainties:
        lines += ["### 分からなかったこと", ""]
        lines += [f"- {u}" for u in r.answer_uncertainties]
        lines.append("")
    return lines


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
