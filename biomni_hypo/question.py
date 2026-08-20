"""研究課題の入力.

「何を調べたいか」を構造化して受け取り、エージェントへ渡すプロンプトを組み立てる。

なぜ自由記述だけにしないか:
  A1 のシステムプロンプトは巨大（16k トークン前後 / docs/design/04 §4.5）で、
  そこに曖昧な一文を足しても、エージェントは何から手を付けるか決められない。
  対象・生物種・知りたい関係を埋めさせるだけで、探索の初手が安定する。

出力するプロンプトは常に確認できる（API の /api/question/preview、UI の「プロンプトを確認」）。
何を投げたか分からないまま結果だけ出てくる、という状態を作らない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class QuestionMode(StrEnum):
    """調べ方の種類。プロンプトの骨格と、期待する出力が変わる。"""

    HYPOTHESIS = "hypothesis"
    EVIDENCE_CHECK = "evidence_check"
    DATA_INTERPRETATION = "data_interpretation"


MODE_LABELS: dict[QuestionMode, str] = {
    QuestionMode.HYPOTHESIS: "仮説生成",
    QuestionMode.EVIDENCE_CHECK: "根拠検証",
    QuestionMode.DATA_INTERPRETATION: "データ解釈",
}

MODE_DESCRIPTIONS: dict[QuestionMode, str] = {
    QuestionMode.HYPOTHESIS: "疑問から、検証可能な仮説を複数出す",
    QuestionMode.EVIDENCE_CHECK: "既にある主張の裏付けと反証を集める",
    QuestionMode.DATA_INTERPRETATION: "アップロードしたデータを解釈する",
}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Hint:
    """入力の質に対する指摘。error があると実行させない。"""

    severity: Severity
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity.value, "field": self.field, "message": self.message}


# --------------------------------------------------------------- テンプレート


@dataclass(frozen=True)
class QuestionTemplate:
    """入力欄の初期値。UI とノートブックの「例から始める」に使う。"""

    id: str
    label: str
    mode: QuestionMode
    text: str
    organism: str = ""
    context: str = ""
    focus: tuple[str, ...] = ()
    background: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mode": self.mode.value,
            "text": self.text,
            "organism": self.organism,
            "context": self.context,
            "focus": list(self.focus),
            "background": self.background,
        }


TEMPLATES: tuple[QuestionTemplate, ...] = (
    QuestionTemplate(
        id="resistance",
        label="治療抵抗性の機序を探す",
        mode=QuestionMode.HYPOTHESIS,
        text="この治療への抵抗性を規定する分子機序の候補を挙げてください。",
        organism="ヒト",
        context="トリプルネガティブ乳がん、PARP 阻害剤（オラパリブ）投与下",
        focus=("BRCA1", "BRCA2", "相同組換え修復"),
        background="BRCA 変異型では奏効するが、非変異型で耐性例が報告されている。",
    ),
    QuestionTemplate(
        id="gene_disease",
        label="遺伝子と疾患の関連を調べる",
        mode=QuestionMode.HYPOTHESIS,
        text="この遺伝子が疾患表現型に寄与する経路の候補を挙げてください。",
        organism="ヒト",
        context="",
        focus=(),
    ),
    QuestionTemplate(
        id="target",
        label="創薬標的の妥当性を評価する",
        mode=QuestionMode.EVIDENCE_CHECK,
        text="この標的を創薬対象とすることの妥当性について、支持する根拠と反証する根拠を集めてください。",
        organism="ヒト",
        context="",
        focus=(),
    ),
    QuestionTemplate(
        id="deg",
        label="発現変動遺伝子リストを解釈する",
        mode=QuestionMode.DATA_INTERPRETATION,
        text="アップロードした発現変動遺伝子リストから、何が起きているかの解釈候補を挙げてください。",
        organism="ヒト",
        context="",
        focus=(),
    ),
    QuestionTemplate(
        id="claim",
        label="論文の主張を検証する",
        mode=QuestionMode.EVIDENCE_CHECK,
        text="次の主張について、公共データと文献から裏付けと反証を集めてください: ",
        organism="ヒト",
        context="",
        focus=(),
    ),
)


# ------------------------------------------------------------------ 入力モデル


class ResearchQuestion(BaseModel):
    """調べたいことの入力。

    必須は `text` のみ。ほかは埋めるほど探索が安定する（埋めないと Hint が出る）。
    """

    text: str = Field(min_length=1, description="調べたいこと（自由記述）")
    mode: QuestionMode = QuestionMode.HYPOTHESIS
    organism: str = Field(default="", description="生物種。例: ヒト, マウス")
    context: str = Field(default="", description="対象。疾患・組織・細胞株・条件")
    focus: list[str] = Field(default_factory=list, description="注目する遺伝子・経路・薬剤")
    background: str = Field(default="", description="既に分かっていること・前提")
    exclude: list[str] = Field(default_factory=list, description="除外したい方向性")
    dataset_ids: list[str] = Field(default_factory=list, description="使う自前データ")
    max_hypotheses: int = Field(default=5, ge=1, le=20)

    @field_validator("text", "organism", "context", "background")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("focus", "exclude", "dataset_ids")
    @classmethod
    def _clean_list(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()]

    # ------------------------------------------------------------- 表示・要約

    @property
    def summary(self) -> str:
        """一覧やレポートの見出しに使う 1 行。"""
        parts = [self.text]
        if self.context:
            parts.append(f"（{self.context}）")
        return "".join(parts)

    # --------------------------------------------------------------- 入力検査

    def hints(self, *, commercial_mode: bool = True) -> list[Hint]:
        """入力の質を検査する。error が 1 つでもあれば実行させない。"""
        out: list[Hint] = []

        if len(self.text) < 10:
            out.append(Hint(Severity.ERROR, "text", "短すぎます。何を知りたいかを一文で書いてください。"))
        elif len(self.text) < 25 and not (self.context or self.focus):
            out.append(
                Hint(Severity.WARNING, "text", "対象や注目遺伝子を書き足すと、探索の初手が安定します。")
            )

        if self.mode is QuestionMode.DATA_INTERPRETATION and not self.dataset_ids:
            out.append(
                Hint(Severity.ERROR, "dataset_ids", "データ解釈モードでは、解析するデータの指定が必要です。")
            )

        if not self.organism:
            out.append(Hint(Severity.WARNING, "organism", "生物種を指定すると、無関係な種のデータを拾いにくくなります。"))

        if not self.context and not self.focus:
            out.append(
                Hint(Severity.WARNING, "context", "疾患・細胞・条件のいずれかを指定してください。範囲が広すぎます。")
            )

        if len(self.focus) > 12:
            out.append(Hint(Severity.WARNING, "focus", "注目対象が多すぎます。1 ランあたり数個に絞るほうが深く調べられます。"))

        if commercial_mode:
            out += excluded_domain_hints(self)

        return out

    @property
    def blocking_hints(self) -> list[Hint]:
        return [h for h in self.hints() if h.severity is Severity.ERROR]

    # ------------------------------------------------------------ プロンプト

    def to_prompt(self, language: str = "en") -> str:
        """エージェントに渡すプロンプトを組み立てる。

        Args:
            language: 指示文（枠組み）の言語。既定は英語。
                A1 のシステムプロンプトもツール説明も英語なので、指示を英語に揃えたほうが
                ローカルモデルの追従が安定する。ユーザーの記述はそのままの言語で埋め込む
                （翻訳しない。訳した時点で意図がずれる）。
        """
        build = _PROMPT_BUILDERS_JA if language == "ja" else _PROMPT_BUILDERS_EN
        return build[self.mode](self)

    def as_spec(self) -> dict[str, Any]:
        """レポート・API 用のシリアライズ。"""
        return self.model_dump(mode="json")

    @classmethod
    def from_text(cls, text: str, **kwargs: Any) -> ResearchQuestion:
        """自由記述だけから作る（後方互換・CLI の簡易入力用）。"""
        return cls(text=text, **kwargs)

    @classmethod
    def from_template(cls, template_id: str) -> ResearchQuestion:
        for t in TEMPLATES:
            if t.id == template_id:
                return cls(
                    text=t.text,
                    mode=t.mode,
                    organism=t.organism,
                    context=t.context,
                    focus=list(t.focus),
                    background=t.background,
                )
        raise KeyError(f"テンプレートがありません: {template_id}")


# --------------------------------------------------- 商用モードで弱い領域の警告

#: 商用モードで除外されるデータセットに依存しがちな話題（docs/design/05 §5.1）
EXCLUDED_DOMAINS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "miRNA",
        ("mirna", "micro rna", "マイクロrna", "ミルナ", "mir-"),
        "miRTarBase / miRDB が商用モードで使えないため、miRNA-標的の解析は弱くなります",
        "商用可の同等データが乏しい領域です。文献ベースの根拠に頼ることになります",
    ),
    (
        "遺伝子セット解析",
        ("gsea", "エンリッチメント", "enrichment", "hallmark", "msigdb", "遺伝子セット", "gene set"),
        "MSigDB が商用モードで使えません",
        "代替: GO (go-plus.json)、Reactome、mousemine の遺伝子セットを使います",
    ),
    (
        "希少疾患・遺伝性疾患",
        ("omim", "希少疾患", "遺伝性疾患", "mendelian", "メンデル"),
        "OMIM / DisGeNET が商用モードで使えません",
        "代替: Open Targets、ClinVar、GWAS Catalog、Monarch を使います",
    ),
    (
        "薬物相互作用",
        ("薬物相互作用", "drug interaction", "ddi", "併用"),
        "DDInter が商用モードで使えません",
        "代替: openFDA で部分的に補います",
    ),
    (
        "結合親和性",
        ("結合親和性", "binding affinity", "ki値", "ic50", "kd値"),
        "BindingDB が商用モードで使えません",
        "代替: ChEMBL（CC BY-SA 3.0）、PubChem を使います",
    ),
)


def excluded_domain_hints(question: ResearchQuestion) -> list[Hint]:
    """商用モードで弱くなる領域に触れていたら知らせる。

    黙って浅い答えを返すより、限界を先に見せるほうが研究用途では価値がある
    （docs/design/05 §5.1 の設計判断）。
    """
    haystack = " ".join(
        [question.text, question.context, question.background, *question.focus]
    ).lower()
    out: list[Hint] = []
    for domain, keywords, problem, alternative in EXCLUDED_DOMAINS:
        if any(k in haystack for k in keywords):
            out.append(
                Hint(Severity.INFO, "commercial_mode", f"{domain}: {problem}。{alternative}。")
            )
    return out


# ------------------------------------------------------------ プロンプト組み立て


def _sections(q: ResearchQuestion, labels: dict[str, str]) -> list[str]:
    lines: list[str] = []
    if q.organism:
        lines.append(f"- {labels['organism']}: {q.organism}")
    if q.context:
        lines.append(f"- {labels['context']}: {q.context}")
    if q.focus:
        lines.append(f"- {labels['focus']}: {', '.join(q.focus)}")
    if q.background:
        lines.append(f"- {labels['background']}: {q.background}")
    if q.exclude:
        lines.append(f"- {labels['exclude']}: {', '.join(q.exclude)}")
    if q.dataset_ids:
        lines.append(f"- {labels['data']}: {', '.join(q.dataset_ids)}")
    return lines


_EN_LABELS = {
    "organism": "Organism",
    "context": "Setting (disease / tissue / cell line / condition)",
    "focus": "Genes, pathways or compounds of interest",
    "background": "What is already known",
    "exclude": "Directions to avoid",
    "data": "User-provided data files",
}

_JA_LABELS = {
    "organism": "生物種",
    "context": "対象（疾患・組織・細胞株・条件）",
    "focus": "注目する遺伝子・経路・化合物",
    "background": "既に分かっていること",
    "exclude": "避けたい方向性",
    "data": "ユーザー提供データ",
}

#: 出力形式の再掲。A1 のシステムプロンプトにも書いてあるが、それは会話の先頭にあり、
#: 手数が増えるほど遠ざかる。指示追従性の低いローカルモデルは平文の計画だけを返して
#: しまい、biomni の generate ノードが「タグが無い」と差し戻して 2 回で打ち切る
#: （docs/design/16 §16.1）。毎回のユーザーメッセージ末尾に置いて近くに保つ。
#: タグはリテラルなので日本語版でも英語のまま出す。
_FORMAT_REMINDER = """
Output format (required, every single turn):
- Write your reasoning first, then EXACTLY ONE of the following tags.
- To run code:  <execute>...python code...</execute>
- To finish:    <solution>...final answer...</solution>
- A reply containing neither tag is discarded. Never write a plan without an <execute> block.
- Never write <observation> yourself; it is filled in for you.
""".strip()

_EN_RULES = (
    """
Rules:
- Ground every claim in data you actually retrieved or computed in this session.
- Prefer querying public databases and the local data lake over recalling facts from memory.
- When you cite a paper or a database record, make sure the identifier appears in the output of code you ran.
- Report contradicting evidence as well; do not only collect support.
""".strip()
    + "\n\n"
    + _FORMAT_REMINDER
)

_JA_RULES = (
    """
守ること:
- 主張は、このセッションで実際に取得・計算した結果に基づかせること。
- 記憶から答えず、公共データベースとローカルのデータレイクを実際に引くこと。
- 文献や DB レコードを引くときは、その識別子が実行結果に現れていること。
- 支持する根拠だけでなく、反証する根拠も報告すること。
""".strip()
    + "\n\n"
    + _FORMAT_REMINDER
)


def _en_hypothesis(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _EN_LABELS))
    return f"""Research question:
{q.text}

{body}

Task: investigate this question and propose up to {q.max_hypotheses} testable hypotheses.
For each hypothesis, identify which data supports it and how it could be experimentally tested.

{_EN_RULES}"""


def _en_evidence_check(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _EN_LABELS))
    return f"""Claim to evaluate:
{q.text}

{body}

Task: gather evidence for and against this claim from public databases and the literature.
Be explicit about which evidence supports it and which contradicts it.
Do not decide the answer before looking at the data.

{_EN_RULES}"""


def _en_data_interpretation(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _EN_LABELS))
    files = ", ".join(q.dataset_ids) or "(none specified)"
    return f"""Interpretation request:
{q.text}

{body}

Task: load and analyse the user-provided data ({files}), then propose up to
{q.max_hypotheses} interpretations of what the data shows.
Every interpretation must cite concrete rows, genes or statistics from the file itself.

{_EN_RULES}"""


def _ja_hypothesis(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _JA_LABELS))
    return f"""研究課題:
{q.text}

{body}

課題: この問いを調査し、検証可能な仮説を最大 {q.max_hypotheses} 件提案してください。
各仮説について、どのデータが支持するか、どう実験で検証できるかを示してください。

{_JA_RULES}"""


def _ja_evidence_check(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _JA_LABELS))
    return f"""検証する主張:
{q.text}

{body}

課題: この主張について、公共データベースと文献から支持する根拠と反証する根拠を集めてください。
データを見る前に結論を決めないこと。

{_JA_RULES}"""


def _ja_data_interpretation(q: ResearchQuestion) -> str:
    body = "\n".join(_sections(q, _JA_LABELS))
    files = ", ".join(q.dataset_ids) or "（未指定）"
    return f"""解釈の依頼:
{q.text}

{body}

課題: ユーザー提供データ（{files}）を読み込んで解析し、
そこから読み取れる解釈を最大 {q.max_hypotheses} 件提案してください。
各解釈は、ファイル中の具体的な行・遺伝子・統計量を根拠にすること。

{_JA_RULES}"""


_PROMPT_BUILDERS_EN = {
    QuestionMode.HYPOTHESIS: _en_hypothesis,
    QuestionMode.EVIDENCE_CHECK: _en_evidence_check,
    QuestionMode.DATA_INTERPRETATION: _en_data_interpretation,
}

_PROMPT_BUILDERS_JA = {
    QuestionMode.HYPOTHESIS: _ja_hypothesis,
    QuestionMode.EVIDENCE_CHECK: _ja_evidence_check,
    QuestionMode.DATA_INTERPRETATION: _ja_data_interpretation,
}


def coerce_question(value: ResearchQuestion | str) -> ResearchQuestion:
    """文字列でも ResearchQuestion でも受け取れるようにする。"""
    if isinstance(value, ResearchQuestion):
        return value
    return ResearchQuestion.from_text(str(value))


def normalise_focus(raw: str) -> list[str]:
    """"BRCA1, BRCA2 / TP53" のような入力をリストにする。UI の 1 行入力用。"""
    return [x.strip() for x in re.split(r"[,、/\n]+", raw) if x.strip()]
