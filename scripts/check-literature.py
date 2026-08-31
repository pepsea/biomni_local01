#!/usr/bin/env python
"""文献検索が本当に動くかを、実際に叩いて確かめる.

    python scripts/check-literature.py                     # 既定の質問で試す
    python scripts/check-literature.py "FGFR1 AND osteoporosis"

「使えるはず」ではなく「使える」を見るためのもの。各ツールについて、
呼べるか・件数・識別子が本文に出ているか・その識別子からリンクを作れるかを出す。
識別子が実行結果に現れないと、根拠として検証できない（docs/design/03）。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from biomni_hypo.citations import extract_citations  # noqa: E402
from biomni_hypo.config import Settings  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402

DEFAULT_QUERY = "FGFR1 AND osteoporosis"

GREEN, RED, YELLOW, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _load(name: str):
    """ツールを取り出す。使えない理由があればそれを返す。"""
    if name == "query_europepmc":
        from biomni_hypo.extra_tools import query_europepmc

        return query_europepmc, ""
    try:
        from biomni.tool import literature
    except ImportError as exc:
        return None, f"biomni を読み込めません: {exc}"
    func = getattr(literature, name, None)
    if func is None:
        return None, f"{name} が biomni.tool.literature にありません"
    return func, ""


def _check(name: str, query: str, policy: ResourcePolicy) -> bool:
    decision = policy.check_tool(name)
    if not decision.allowed:
        print(f"  {YELLOW}−{OFF} {name:18s} ポリシーで不可: {decision.reason}")
        return True                      # 意図的に外している。失敗ではない

    func, why = _load(name)
    if func is None:
        print(f"  {RED}✗{OFF} {name:18s} {why}")
        return False

    started = time.monotonic()
    try:
        out = func(query)
    except Exception as exc:  # noqa: BLE001 - 何が起きたかを出すのが仕事
        print(f"  {RED}✗{OFF} {name:18s} {type(exc).__name__}: {exc}")
        return False
    took = time.monotonic() - started
    text = out if isinstance(out, str) else str(out)

    # biomni のツールは "Error querying PubMed: ..." のように返すものもある。
    # "Error:" だけを見ていると、失敗を「識別子が出ていない」と誤分類する
    if text.lstrip().lower().startswith("error"):
        print(f"  {RED}✗{OFF} {name:18s} {text.strip()[:150]}")
        return False

    citations = extract_citations(text, step_idx=0, tools_in_step=[name])
    linkable = [c for c in citations if c.url]
    if not citations:
        print(f"  {YELLOW}!{OFF} {name:18s} 応答はあるが識別子が本文に出ていません"
              f"（{len(text)} 文字, {took:.1f}s）")
        print(f"      先頭: {text.strip()[:120]}")
        return False

    print(f"  {GREEN}✓{OFF} {name:18s} 識別子 {len(citations)} 件 / "
          f"リンク可 {len(linkable)} 件（{took:.1f}s）")
    for c in linkable[:3]:
        print(f"      {c.identifier:34s} {c.url}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    query = args[0] if args else DEFAULT_QUERY
    settings = Settings()
    policy = ResourcePolicy.load(settings.policy_path)

    print(f"\n\033[1m== 文献検索の確認\033[0m\n  検索語: {query!r}\n")
    ok = [_check(name, query, policy)
          for name in ("query_pubmed", "query_europepmc", "query_arxiv",
                       "query_scholar")]

    print()
    if all(ok):
        print(f"  {GREEN}すべて動いています。{OFF}")
        return 0
    print(f"  {RED}動いていないものがあります。{OFF}上の理由を見てください。")
    print("      ネットワークに出られない環境では、すべて失敗します。")
    print("      pymed / arxiv が無いと PubMed / arXiv は使えません:")
    print("          pip install pymed arxiv")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
