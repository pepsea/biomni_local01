#!/usr/bin/env python
"""コマンドラインから調べたいことを入力して仮説を構築する.

    python scripts/ask.py                              # 対話入力
    python scripts/ask.py "TNBC の PARP 阻害剤耐性は？" --organism ヒト
    python scripts/ask.py --template resistance --dry-run   # プロンプトだけ見る

Web アプリの POST /api/runs と同じ関数（run_hypothesis）を呼ぶ。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from biomni_hypo.config import Settings  # noqa: E402
from biomni_hypo.models import ModelNotAvailable, apply_model_selection, list_local_models  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402
from biomni_hypo.question import (  # noqa: E402
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    TEMPLATES,
    QuestionMode,
    ResearchQuestion,
    Severity,
    normalise_focus,
)

MARK = {Severity.ERROR: "❌", Severity.WARNING: "⚠️ ", Severity.INFO: "ℹ️ "}


def prompt_interactively(defaults: ResearchQuestion | None = None) -> ResearchQuestion:
    """対話で 1 項目ずつ埋める。Enter で既定値/空のまま。"""
    d = defaults

    print("\n調べ方を選んでください:")
    modes = list(QuestionMode)
    for i, m in enumerate(modes, 1):
        print(f"  {i}. {MODE_LABELS[m]} — {MODE_DESCRIPTIONS[m]}")
    raw = input(f"番号 [{1 if d is None else modes.index(d.mode) + 1}]: ").strip()
    mode = modes[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(modes) else (d.mode if d else modes[0])

    def ask(label: str, current: str = "", required: bool = False) -> str:
        suffix = f" [{current}]" if current else ("" if required else " （空可）")
        while True:
            value = input(f"{label}{suffix}: ").strip() or current
            if value or not required:
                return value
            print("  必須です。")

    text = ask("調べたいこと", d.text if d else "", required=True)
    organism = ask("生物種", d.organism if d else "ヒト")
    context = ask("対象（疾患・組織・細胞株・条件）", d.context if d else "")
    focus = normalise_focus(ask("注目する遺伝子・経路・薬剤（カンマ区切り）", ", ".join(d.focus) if d else ""))
    background = ask("既に分かっていること", d.background if d else "")

    return ResearchQuestion(
        text=text, mode=mode, organism=organism, context=context,
        focus=focus, background=background,
    )


def build_from_args(args: argparse.Namespace) -> ResearchQuestion:
    base = ResearchQuestion.from_template(args.template) if args.template else None

    if args.question:
        return ResearchQuestion(
            text=args.question,
            mode=QuestionMode(args.mode),
            organism=args.organism or (base.organism if base else ""),
            context=args.context or (base.context if base else ""),
            focus=normalise_focus(args.focus) or (list(base.focus) if base else []),
            background=args.background or (base.background if base else ""),
            dataset_ids=args.data or [],
            max_hypotheses=args.max_hypotheses,
        )
    if base and args.yes:
        return base
    return prompt_interactively(base)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="?", help="調べたいこと（省略すると対話入力）")
    parser.add_argument("--mode", choices=[m.value for m in QuestionMode], default="hypothesis")
    parser.add_argument("--template", help=f"例から始める: {', '.join(t.id for t in TEMPLATES)}")
    parser.add_argument("--organism", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--focus", default="", help="カンマ区切り")
    parser.add_argument("--background", default="")
    parser.add_argument("--data", nargs="*", help="解析する自前データのファイル名")
    parser.add_argument("--max-hypotheses", type=int, default=5)
    parser.add_argument("--model", help="使うモデル（省略時はローカルから既定を選ぶ）")
    parser.add_argument("--dry-run", action="store_true", help="プロンプトと指摘だけ表示して実行しない")
    parser.add_argument("--yes", "-y", action="store_true", help="確認を省いて実行する")
    parser.add_argument("--out", help="レポートの保存先（.md）")
    args = parser.parse_args()

    settings = Settings()
    policy = ResourcePolicy.load(settings.policy_path)
    question = build_from_args(args)

    print("\n" + "=" * 68)
    print(f"モード : {MODE_LABELS[question.mode]}")
    print(f"課題   : {question.summary}")

    hints = question.hints(commercial_mode=settings.commercial_mode)
    if hints:
        print()
        for h in hints:
            print(f"{MARK[h.severity]} {h.message}")
    if question.blocking_hints:
        print("\n入力に問題があります。修正してください。", file=sys.stderr)
        return 1

    prompt = question.to_prompt(settings.prompt_language)
    print("\n--- エージェントに渡すプロンプト ---")
    print(prompt)
    print("=" * 68)

    if args.dry_run:
        return 0

    catalog = list_local_models(settings, policy)
    try:
        _catalog, notes = apply_model_selection(settings, policy, model=args.model, catalog=catalog)
    except ModelNotAvailable as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        print("\n" + catalog.as_table(), file=sys.stderr)
        return 1
    for note in notes:
        print(f"⚠️  {note}")
    print(f"\nモデル: {settings.model} / num_ctx {settings.num_ctx:,}")

    if not args.yes and input("\n実行しますか？ [Y/n]: ").strip().lower() in ("n", "no"):
        return 0

    from biomni_hypo.pipeline import run_hypothesis, summarize
    from biomni_hypo.report import to_markdown

    def on_event(kind: str, payload: dict) -> None:
        if kind == "phase":
            print(f"\n── {payload['phase']} ──")
        elif kind == "step":
            head = (payload.get("code") or payload.get("text") or "").strip().replace("\n", " ")
            print(f"[{payload['idx']:2d}] {payload['kind']:15s} {head[:80]}")

    result = run_hypothesis(question, settings=settings, policy=policy, on_event=on_event)
    print("\n" + summarize(result))

    out = pathlib.Path(args.out) if args.out else pathlib.Path(settings.workspace_path) / "reports" / f"{result.id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(result), encoding="utf-8")
    print(f"レポート: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
