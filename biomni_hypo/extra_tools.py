"""biomni に無い文献検索を足す.

biomni の文献ツールは PubMed / arXiv / Google Scholar の 3 つですが、
後ろ 2 つは使えません。

- `query_scholar` / `search_google` は商用ポリシーで拒否しています。
  Google Scholar には公式 API が無く、`scholarly` は画面をスクレイプします。
  ToS の問題があり、実運用では CAPTCHA で止まります（config/resource_policy.yaml）。
- `query_arxiv` は生物医学の主要誌を収載していません。

残るのは PubMed だけで、これは 1 つの情報源です。§3 で「独立した情報源を
複数当たること」と決めているのに、文献に関してはそれができていませんでした。

Europe PMC を足します。公開 REST API があり、鍵も要りません。
PubMed の内容に加えて、プレプリント（bioRxiv / medRxiv）、特許、
Agricola、書籍を収載しており、**PubMed に無い文献が引けます**。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: 1 件あたりの抄録の長さ。全文を入れると 1 回で文脈を食い潰す（docs/design/40）
_ABSTRACT_CHARS = 700


def query_europepmc(query: str, max_papers: int = 5, timeout: float = 30.0) -> str:
    """Search Europe PMC for papers and return their titles, IDs and abstracts.

    Europe PMC covers PubMed plus preprints (bioRxiv, medRxiv), patents and
    books, so it finds papers PubMed alone does not.

    Parameters
    ----------
    query (str, required): Search terms, e.g. "FGFR1 AND osteoporosis"
    max_papers (int): How many results to return (default 5)

    Returns
    -------
    str: One block per paper with Title, IDs (PMID / PMCID / DOI) and Abstract.
    """
    params = {
        "query": query,
        "format": "json",
        "pageSize": max(1, min(int(max_papers or 5), 25)),
        "resultType": "core",
    }
    try:
        response = requests.get(EUROPEPMC_SEARCH, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - 観測として返すのがこの関数の仕事
        return f"Error: Europe PMC query failed: {type(exc).__name__}: {exc}"

    results = ((payload or {}).get("resultList") or {}).get("result") or []
    if not results:
        return f"No results in Europe PMC for: {query}"
    return "\n\n".join(_format(item) for item in results)


def _format(item: dict[str, Any]) -> str:
    """1 件を、識別子が本文に現れる形で書く。

    識別子が実行結果に現れていないと、根拠として検証できない（docs/design/03）。
    """
    ids = []
    if item.get("pmid"):
        ids.append(f"PMID:{item['pmid']}")
    if item.get("pmcid"):
        ids.append(str(item["pmcid"]))
    if item.get("doi"):
        ids.append(f"DOI:{item['doi']}")
    abstract = (item.get("abstractText") or "").strip()
    if len(abstract) > _ABSTRACT_CHARS:
        abstract = abstract[:_ABSTRACT_CHARS] + "…"
    lines = [f"Title: {item.get('title', '(no title)').strip()}"]
    if ids:
        lines.append("IDs: " + " ".join(ids))
    source = " ".join(
        str(item[key]) for key in ("journalTitle", "pubYear") if item.get(key)
    ).strip()
    if source:
        lines.append(f"Source: {source}")
    lines.append(f"Abstract: {abstract or '(no abstract)'}")
    return "\n".join(lines)


#: biomni の module2api に足すスキーマ。形式は read_module2api() と同じ
EUROPEPMC_SCHEMA: dict[str, Any] = {
    "name": "query_europepmc",
    "description": (
        "Search Europe PMC for biomedical papers. Covers PubMed plus preprints "
        "(bioRxiv, medRxiv), patents and books, so it finds papers PubMed misses. "
        "Returns titles, identifiers (PMID / PMCID / DOI) and abstracts."
    ),
    "required_parameters": [
        {
            "name": "query",
            "type": "str",
            "description": "Search terms, e.g. 'FGFR1 AND osteoporosis'",
            "default": None,
        }
    ],
    "optional_parameters": [
        {
            "name": "max_papers",
            "type": "int",
            "description": "How many results to return",
            "default": 5,
        }
    ],
}

#: module2api に入れるときのモジュール名。biomni のものと区別できる名前にする
MODULE_NAME = "biomni_hypo.extra_tools"

EXTRA_TOOLS: dict[str, Any] = {"query_europepmc": query_europepmc}
EXTRA_SCHEMAS: list[dict[str, Any]] = [EUROPEPMC_SCHEMA]
