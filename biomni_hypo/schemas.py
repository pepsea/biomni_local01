"""ドメインモデル.

docs/design/03-evidence-model.md のデータモデルに対応する。
SQL の永続化層（Web アプリ）とノートブックの両方でこの型を使う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StepKind(StrEnum):
    THINK = "think"
    EXECUTE = "execute"
    OBSERVATION = "observation"
    SOLUTION = "solution"
    POLICY_BLOCKED = "policy_blocked"
    #: 解析の計画（biomni が最初に立てさせるチェックリスト）
    PLAN = "plan"
    #: モデルが <execute> / <solution> のどちらも出さず、biomni が差し戻した
    PARSING_ERROR = "parsing_error"
    ERROR = "error"


class ResourceKind(StrEnum):
    DATASET = "dataset"
    USER_FILE = "user_file"
    TOOL = "tool"
    LIBRARY = "library"
    LITERATURE = "literature"
    DB_RECORD = "db_record"
    COMPUTATION = "computation"
    KNOW_HOW = "know_how"


class Stance(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    CONTEXT = "context"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class ToolCall(BaseModel):
    name: str
    module: str = ""


class PlanItem(BaseModel):
    """解析計画のチェックリスト 1 行。

    biomni は「まず計画を立てろ」と指示しており（a1.py の system prompt）、
    `1. [ ] ...` / `2. [✓] ...` / `3. [✗] ... (failed because...)` の形で
    毎ターン更新させる。ここはその 1 行。
    """

    text: str
    state: Literal["todo", "done", "failed"] = "todo"
    #: 失敗した理由（"(failed because ...)" の部分）
    note: str = ""


class Step(BaseModel):
    """エージェントの 1 手。"""

    model_config = ConfigDict(use_enum_values=False)

    idx: int
    kind: StepKind
    text: str = ""
    code: str = ""
    tools: list[ToolCall] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    user_files: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    #: kind == PLAN のときの計画の中身
    plan: list[PlanItem] = Field(default_factory=list)
    duration_ms: int = 0
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class Artifact(BaseModel):
    id: str
    kind: Literal["image", "file"] = "image"
    mime: str = "image/png"
    data_b64: str = ""
    path: str = ""


class Citation(BaseModel):
    """observation から機械抽出した識別子。まだ仮説には紐付いていない。"""

    kind: ResourceKind
    identifier: str
    excerpt: str = ""
    step_idx: int = -1
    url: str = ""


class EvidenceCandidate(BaseModel):
    """Extractor に「この ID からしか選べない」形で渡す根拠候補。"""

    eid: str
    kind: ResourceKind
    identifier: str
    excerpt: str = ""
    step_idx: int = -1
    url: str = ""

    def as_prompt_line(self) -> str:
        excerpt = self.excerpt.replace("\n", " ")[:240]
        return f"{self.eid} | {self.kind.value} | {self.identifier} | step {self.step_idx} | {excerpt}"


class Resource(BaseModel):
    """根拠の出所。ライセンス情報を持つ（docs/design/05）。"""

    kind: ResourceKind
    name: str
    identifier: str = ""
    url: str = ""
    license: str = "unknown"
    attribution: str = ""
    commercial_ok: bool | None = None
    review_required: bool = False
    step_idxs: list[int] = Field(default_factory=list)


class Evidence(BaseModel):
    eid: str
    kind: ResourceKind
    identifier: str
    stance: Stance = Stance.SUPPORTS
    claim_span: str = ""
    why: str = ""
    excerpt: str = ""
    step_idx: int = -1
    url: str = ""
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_note: str = ""
    strength: float = 0.5


class ReasoningPoint(BaseModel):
    """最終回答に至った論点 1 つ。

    biomni の `<solution>` は既定では「採点できる短い答え」を返す設計で
    （システムプロンプトの唯一の例が `The answer is <solution> A </solution>`）、
    そこに至った筋道は残らない。結論だけ見せても、読んだ人はそれを
    受け入れるか捨てるかしか選べない。

    「何を確かめたか → 何が分かったか → それが結論をどう左右したか」を
    1 単位にして、結論の組み立てを開く。根拠は他と同じ検証を通す。
    """

    #: 検討した論点。問いの形で書く（例:「FGFR2 の関連は再現しているか」）
    point: str
    #: 調べて分かったこと
    finding: str = ""
    #: この論点が結論を支持するか、反証するか、判断材料にとどまるか
    stance: Stance = Stance.SUPPORTS
    #: 結論への効き方。decisive = これが無ければ結論が変わる
    weight: Literal["decisive", "supporting", "weak"] = "supporting"
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        return any(
            e.verification_status in (VerificationStatus.VERIFIED, VerificationStatus.NOT_APPLICABLE)
            for e in self.evidence
        )


class TestPlan(BaseModel):
    experiment: str = ""
    readout: str = ""
    controls: list[str] = Field(default_factory=list)
    feasibility: Literal["high", "medium", "low"] = "medium"
    estimated_effort: str = ""


class Hypothesis(BaseModel):
    id: str = ""
    statement: str
    rationale: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"
    novelty: Literal["established", "emerging", "speculative"] = "emerging"
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    test_plan: TestPlan = Field(default_factory=TestPlan)

    @property
    def is_supported(self) -> bool:
        """検証を通った根拠が 1 件以上あるか。"""
        return any(
            e.verification_status in (VerificationStatus.VERIFIED, VerificationStatus.NOT_APPLICABLE)
            for e in self.evidence
        )


class FailedCitation(BaseModel):
    identifier: str
    kind: ResourceKind
    reason: str
    step_idx: int = -1


class VerificationSummary(BaseModel):
    verified: int = 0
    unverified: int = 0
    failed: int = 0
    not_applicable: int = 0

    @property
    def rate(self) -> float:
        """引用検証率 = verified / (verified + failed)。分母 0 なら 1.0。"""
        denom = self.verified + self.failed
        return 1.0 if denom == 0 else self.verified / denom


class RunConfig(BaseModel):
    """1 ランの再現に必要な設定すべて。レポートにそのまま載る。"""

    provider: str = "ollama"
    model: str = "qwen3:14b"
    temperature: float = 0.7
    num_ctx: int = 32768
    num_predict: int = 4096
    ollama_base_url: str = "http://localhost:11434"
    data_path: str = "./data"
    timeout_seconds: int = 600
    max_steps: int = 60
    wallclock_limit_sec: int = 1800
    max_hypotheses: int = 5
    use_tool_retriever: bool = False
    commercial_mode: bool = True
    offline_mode: bool = False
    policy_version: int = 0
    biomni_version: str = ""


class RunResult(BaseModel):
    """1 ランの全出力。API レスポンスとレポートの共通ソース。"""

    id: str
    #: 人が読む形の課題（ResearchQuestion.summary）
    question: str
    #: 構造化された入力（ResearchQuestion.as_spec()）
    question_spec: dict[str, Any] = Field(default_factory=dict)
    #: エージェントに実際に渡したプロンプト。何を投げたかを必ず残す
    prompt: str = ""
    status: Literal["running", "succeeded", "failed", "cancelled"] = "running"
    config: RunConfig = Field(default_factory=RunConfig)
    #: 最新の解析計画（毎ターン更新されるので最後のものを残す）
    plan: list[PlanItem] = Field(default_factory=list)
    #: 計画が書き直された回数（初回を除く）
    plan_revisions: int = 0
    resources_considered: dict[str, list[str]] = Field(default_factory=dict)
    resources_used: list[Resource] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    solution_text: str = ""
    #: 質問への直接の回答（根拠に紐付いた要約）
    answer: str = ""
    answer_evidence: list[Evidence] = Field(default_factory=list)
    #: 回答に至った論点。結論だけでなく組み立てを見せる（docs/design/18）
    answer_reasoning: list[ReasoningPoint] = Field(default_factory=list)
    #: 調べたが分からなかったこと・この回答の限界
    answer_uncertainties: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    unsupported_ideas: list[Hypothesis] = Field(default_factory=list)
    failed_citations: list[FailedCitation] = Field(default_factory=list)
    verification: VerificationSummary = Field(default_factory=VerificationSummary)
    error: str = ""
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


Step.model_rebuild()
