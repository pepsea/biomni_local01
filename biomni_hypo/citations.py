"""observation テキストからの識別子抽出.

docs/design/03-evidence-model.md §3.5 に対応。

方針: 拾いは広めでよい。過検出は verifier.py の実在検証で落ちる。
ただし誤検出が特に多いパターン（PDB の 4 文字など）は、
「そのステップで該当ツールが呼ばれていたか」でゲートする。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from biomni_hypo.schemas import Citation, ResourceKind, ToolCall


@dataclass(frozen=True)
class Pattern:
    kind: ResourceKind
    name: str
    regex: re.Pattern[str]
    #: このパターンを有効化するために必要なツール名（空なら常に有効）
    gate_tools: frozenset[str] = frozenset()
    #: マッチを識別子へ正規化するときの接頭辞
    prefix: str = ""
    url_template: str = ""


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        ResourceKind.LITERATURE,
        "pmid",
        re.compile(r"(?:PMID[:\s]*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{7,8})\b", re.IGNORECASE),
        prefix="PMID:",
        url_template="https://pubmed.ncbi.nlm.nih.gov/{id}/",
    ),
    Pattern(
        ResourceKind.LITERATURE,
        "doi",
        re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]*[A-Za-z0-9])"),
        prefix="DOI:",
        url_template="https://doi.org/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "ensembl",
        re.compile(r"\b(ENS[A-Z]{0,4}[GTP]\d{11})\b"),
        url_template="https://www.ensembl.org/id/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "uniprot",
        re.compile(
            r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b"
        ),
        gate_tools=frozenset({"query_uniprot", "query_alphafold", "query_interpro"}),
        url_template="https://www.uniprot.org/uniprotkb/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "dbsnp",
        re.compile(r"\b(rs\d{3,})\b"),
        url_template="https://www.ncbi.nlm.nih.gov/snp/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "geo",
        re.compile(r"\b(GS[EM]\d{3,})\b"),
        url_template="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "clinvar",
        re.compile(r"\b([VR]CV\d{6,})\b"),
        url_template="https://www.ncbi.nlm.nih.gov/clinvar/variation/{id}/",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "chembl",
        re.compile(r"\b(CHEMBL\d+)\b"),
        url_template="https://www.ebi.ac.uk/chembl/compound_report_card/{id}/",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "reactome",
        re.compile(r"\b(R-[A-Z]{3}-\d+)\b"),
        url_template="https://reactome.org/content/detail/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "go_term",
        re.compile(r"\b(GO:\d{7})\b"),
        url_template="https://amigo.geneontology.org/amigo/term/{id}",
    ),
    Pattern(
        ResourceKind.DB_RECORD,
        "pdb",
        # 4 文字は英単語と衝突するため、PDB ツールを呼んだステップでのみ有効化する
        re.compile(r"\b([1-9][A-Za-z0-9]{3})\b"),
        gate_tools=frozenset({"query_pdb", "query_pdb_identifiers", "query_emdb"}),
        url_template="https://www.rcsb.org/structure/{id}",
    ),
)

#: 上のパターンから漏れる、明らかに識別子でない語（PDB ゲート時の保険）
_PDB_STOPWORDS = frozenset({"2024", "2025", "2026", "1000", "0000"})

EXCERPT_WINDOW = 120


def _excerpt(text: str, start: int, end: int, window: int = EXCERPT_WINDOW) -> str:
    """識別子の前後を実テキストから切り出す。

    LLM に抜粋を書かせないための関数。ここが幻覚耐性の要になる。
    """
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    snippet = text[lo:hi].strip()
    snippet = re.sub(r"\s+", " ", snippet)
    if lo > 0:
        snippet = "…" + snippet
    if hi < len(text):
        snippet = snippet + "…"
    return snippet


def extract_citations(
    text: str,
    *,
    step_idx: int = -1,
    tools_in_step: Iterable[ToolCall | str] = (),
    max_per_pattern: int = 50,
) -> list[Citation]:
    """1 つの observation テキストから識別子を抽出する。

    Args:
        text: observation の生テキスト。
        step_idx: 由来ステップ番号。
        tools_in_step: そのステップで呼ばれたツール。ゲート付きパターンの有効化に使う。
        max_per_pattern: 1 パターンあたりの上限（巨大な出力での暴走防止）。
    """
    if not text:
        return []

    tool_names = {t.name if isinstance(t, ToolCall) else str(t) for t in tools_in_step}
    seen: set[tuple[str, str]] = set()
    out: list[Citation] = []

    for pat in PATTERNS:
        if pat.gate_tools and not (pat.gate_tools & tool_names):
            continue
        count = 0
        for m in pat.regex.finditer(text):
            raw = m.group(1)
            if pat.name == "pdb" and raw in _PDB_STOPWORDS:
                continue
            identifier = f"{pat.prefix}{raw}"
            key = (pat.kind.value, identifier.upper())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Citation(
                    kind=pat.kind,
                    identifier=identifier,
                    excerpt=_excerpt(text, m.start(1), m.end(1)),
                    step_idx=step_idx,
                    url=pat.url_template.format(id=raw) if pat.url_template else "",
                )
            )
            count += 1
            if count >= max_per_pattern:
                break
    return out


def bare_identifier(identifier: str) -> str:
    """"PMID:123" -> "123"。検証時に接頭辞を落とす。"""
    return identifier.split(":", 1)[1] if ":" in identifier and not identifier.startswith("GO:") else identifier
