"""根拠の実在検証（docs/design/03-evidence-model.md §3.4）.

LLM を一切使わずに検証する。ここが本アプリの信頼性の要。
検証を通らなかった根拠は仮説から切り離し、failed_citations に隔離する。

包含チェック（C ⊆ B）が最重要:
  「主張を支えた」根拠が「実際にコードで触れた」ものに含まれていなければ、
  それはトレースに存在しない出所であり、幻覚として無条件に落とす。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import requests

from biomni_hypo.citations import bare_identifier
from biomni_hypo.schemas import (
    Evidence,
    FailedCitation,
    Hypothesis,
    ResourceKind,
    Step,
    StepKind,
    VerificationStatus,
    VerificationSummary,
)

log = logging.getLogger(__name__)

PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


@dataclass
class TraceIndex:
    """トレース側の事実。検証はすべてこれとの突き合わせで行う。"""

    observation_text: dict[int, str] = field(default_factory=dict)
    code_text: dict[int, str] = field(default_factory=dict)
    datasets_touched: set[str] = field(default_factory=set)
    user_files_touched: set[str] = field(default_factory=set)
    executed_steps: set[int] = field(default_factory=set)
    failed_steps: set[int] = field(default_factory=set)

    @classmethod
    def from_steps(cls, steps: Iterable[Step]) -> TraceIndex:
        idx = cls()
        for s in steps:
            if s.kind == StepKind.OBSERVATION:
                idx.observation_text[s.idx] = s.text
            elif s.kind == StepKind.EXECUTE:
                idx.code_text[s.idx] = s.code
                idx.datasets_touched.update(s.datasets)
                idx.user_files_touched.update(s.user_files)
                (idx.failed_steps if s.error else idx.executed_steps).add(s.idx)
        return idx

    @property
    def all_observation_text(self) -> str:
        return "\n".join(self.observation_text.values())


@dataclass
class VerificationReport:
    summary: VerificationSummary = field(default_factory=VerificationSummary)
    failed: list[FailedCitation] = field(default_factory=list)


class EvidenceVerifier:
    """根拠を検証する。ネットワーク呼び出しは差し替え可能（テスト用）。"""

    def __init__(
        self,
        *,
        offline: bool = False,
        pmid_checker: Callable[[str], tuple[bool | None, str]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.offline = offline
        self.timeout = timeout
        self._pmid_checker = pmid_checker or self._check_pmid_online
        self._pmid_cache: dict[str, tuple[bool | None, str]] = {}

    # ------------------------------------------------------------------ API

    def verify_run(
        self, hypotheses: list[Hypothesis], steps: Iterable[Step]
    ) -> tuple[list[Hypothesis], list[Hypothesis], VerificationReport]:
        """仮説群を検証し、(裏付けあり, 未裏付け, レポート) に分ける。

        検証に失敗した根拠は仮説から取り除かれる。仮説自体は消さず、
        根拠がゼロになったものを「未裏付けの着想」に回す。
        """
        index = TraceIndex.from_steps(steps)
        report = VerificationReport()

        for h in hypotheses:
            kept: list[Evidence] = []
            for ev in h.evidence:
                status, note = self.verify_evidence(ev, index)
                ev.verification_status = status
                ev.verification_note = note
                _count(report.summary, status)
                if status == VerificationStatus.FAILED:
                    report.failed.append(
                        FailedCitation(
                            identifier=ev.identifier, kind=ev.kind, reason=note, step_idx=ev.step_idx
                        )
                    )
                else:
                    kept.append(ev)
            h.evidence = kept

        supported = [h for h in hypotheses if h.is_supported]
        unsupported = [h for h in hypotheses if not h.is_supported]
        return supported, unsupported, report

    def verify_evidence(self, ev: Evidence, index: TraceIndex) -> tuple[VerificationStatus, str]:
        # --- 包含チェック（C ⊆ B）。すべての種別に先に効かせる ------------------
        if ev.kind in (ResourceKind.LITERATURE, ResourceKind.DB_RECORD):
            if not self._appears_in_trace(ev, index):
                return (
                    VerificationStatus.FAILED,
                    "トレースの実行結果に出現しない識別子です（幻覚の可能性）",
                )

        if ev.kind == ResourceKind.DATASET:
            if ev.identifier not in index.datasets_touched:
                return VerificationStatus.FAILED, "コード中で読み込まれていないデータセットです"
            return VerificationStatus.VERIFIED, "コードでの読み込みを確認"

        if ev.kind == ResourceKind.USER_FILE:
            if ev.identifier not in index.user_files_touched:
                return VerificationStatus.FAILED, "コード中で読み込まれていないファイルです"
            return VerificationStatus.VERIFIED, "コードでの読み込みを確認"

        if ev.kind == ResourceKind.COMPUTATION:
            step_idx = _step_number(ev.identifier)
            if step_idx in index.failed_steps:
                return VerificationStatus.FAILED, "エラーで終了した実行を根拠にしています"
            if step_idx not in index.executed_steps:
                return VerificationStatus.FAILED, "対応する実行ステップがありません"
            return VerificationStatus.VERIFIED, "実行ステップの存在を確認"

        if ev.kind == ResourceKind.LITERATURE:
            if ev.identifier.upper().startswith("PMID:"):
                if self.offline:
                    return VerificationStatus.NOT_APPLICABLE, "オフラインモードのため外部検証を省略"
                ok, note = self._pmid(bare_identifier(ev.identifier))
                if ok is None:
                    # ネットワーク起因で確認できなかっただけ。「検証失敗」と混同しない。
                    return VerificationStatus.NOT_APPLICABLE, note
                return (VerificationStatus.VERIFIED if ok else VerificationStatus.FAILED), note
            # DOI はトレース内出現の確認まで（HEAD リクエストは任意）
            return VerificationStatus.UNVERIFIED, "実行結果への出現のみ確認（外部検証は未実施）"

        if ev.kind == ResourceKind.DB_RECORD:
            return VerificationStatus.VERIFIED, "実行結果への出現を確認"

        return VerificationStatus.UNVERIFIED, "検証方法が定義されていない種別です"

    # -------------------------------------------------------------- 内部処理

    def _appears_in_trace(self, ev: Evidence, index: TraceIndex) -> bool:
        """識別子が observation の実テキストに出現するか。

        由来ステップが記録されていればそこを優先し、無ければ全 observation を見る。
        """
        needle = bare_identifier(ev.identifier)
        target = index.observation_text.get(ev.step_idx)
        haystack = target if target else index.all_observation_text
        return bool(needle) and needle.lower() in haystack.lower()

    def _pmid(self, pmid: str) -> tuple[bool | None, str]:
        if pmid in self._pmid_cache:
            return self._pmid_cache[pmid]
        result = self._pmid_checker(pmid)
        self._pmid_cache[pmid] = result
        return result

    def _check_pmid_online(self, pmid: str) -> tuple[bool | None, str]:
        """NCBI E-utilities で PMID の実在を確認し、タイトルを取得する。

        戻り値の第 1 要素: True=実在 / False=存在しない / None=確認できなかった。
        ネットワーク障害を「捏造」と誤判定しないための三値。
        """
        try:
            r = requests.get(
                PUBMED_ESUMMARY,
                params={"db": "pubmed", "id": pmid, "retmode": "json"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            payload = r.json().get("result", {})
            entry = payload.get(pmid)
            if not entry or "error" in entry:
                return False, "PubMed に存在しない PMID です"
            title = str(entry.get("title", "")).strip()
            return True, f"PubMed で確認: {title[:120]}" if title else "PubMed で確認"
        except Exception as exc:  # noqa: BLE001 - ネットワーク起因は検証不能として扱う
            log.warning("PMID 検証に失敗 (%s): %s", pmid, exc)
            return None, f"外部検証を実施できませんでした（{type(exc).__name__}）"


def _count(summary: VerificationSummary, status: VerificationStatus) -> None:
    setattr(summary, status.value, getattr(summary, status.value) + 1)


def _step_number(identifier: str) -> int:
    m = re.search(r"(\d+)", identifier)
    return int(m.group(1)) if m else -1
