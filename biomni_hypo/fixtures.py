"""オフライン検証用のフィクスチャ.

Ollama も biomni も無い環境（CI、ノートブックの初回実行）で、
抽出 -> 検証 -> レポートのパイプラインを最後まで通すためのもの。
本番コードからは import しない。
"""

from __future__ import annotations

import json
from typing import Any

from biomni_hypo.citations import extract_citations
from biomni_hypo.schemas import Step, StepKind, ToolCall

OBSERVATION_GWAS = """\
Query: breast carcinoma (EFO_0000305)
  rs2981582   FGFR2    breast carcinoma   p=2e-76   OR=1.26   PMID: 17529967
  rs889312    MAP3K1   breast carcinoma   p=7e-20   OR=1.13   PMID: 17529967
  rs13387042  ENSG00000138675            p=2e-16
Retrieved 128 associations from gwas_catalog.pkl
"""

OBSERVATION_DEPMAP = """\
FGFR2 dependency (DepMap_CRISPRGeneEffect.csv)
  mean gene effect in breast lineage: -0.41 (n=52)
  strongest dependency: ACH-000019 (-0.88)
Correlation with PARPi (olaparib) resistance annotation: rho=0.31, p=0.02
"""

OBSERVATION_LITERATURE = """\
1. FGFR signalling and therapy resistance in triple-negative breast cancer. PMID: 31234567
2. A study reporting no association between FGFR2 and PARP inhibitor response. PMID: 28123456
"""


def sample_steps() -> list[Step]:
    """典型的なトレースを 1 本組み立てる（GWAS -> DepMap -> 文献）。"""
    steps: list[Step] = []

    steps.append(Step(idx=0, kind=StepKind.THINK, text="まず GWAS Catalog で乳がん関連座位を確認する。"))

    code1 = (
        "from biomni.tool.database import query_gwas_catalog\n"
        "import pandas as pd\n"
        "gwas = pd.read_pickle('gwas_catalog.pkl')\n"
        "res = query_gwas_catalog('breast carcinoma')\n"
        "print(res)\n"
    )
    steps.append(
        Step(
            idx=1,
            kind=StepKind.EXECUTE,
            code=code1,
            tools=[ToolCall(name="query_gwas_catalog", module="biomni.tool.database")],
            datasets=["gwas_catalog.pkl"],
            duration_ms=4210,
        )
    )
    steps.append(
        Step(
            idx=2,
            kind=StepKind.OBSERVATION,
            text=OBSERVATION_GWAS,
            citations=extract_citations(OBSERVATION_GWAS, step_idx=2),
        )
    )

    code2 = (
        "import pandas as pd\n"
        "dep = pd.read_csv('DepMap_CRISPRGeneEffect.csv')\n"
        "print(dep.filter(like='FGFR2').describe())\n"
    )
    steps.append(
        Step(
            idx=3,
            kind=StepKind.EXECUTE,
            code=code2,
            datasets=["DepMap_CRISPRGeneEffect.csv"],
            duration_ms=8900,
        )
    )
    steps.append(
        Step(
            idx=4,
            kind=StepKind.OBSERVATION,
            text=OBSERVATION_DEPMAP,
            citations=extract_citations(OBSERVATION_DEPMAP, step_idx=4),
        )
    )

    code3 = (
        "from biomni.tool.literature import query_pubmed\n"
        "print(query_pubmed('FGFR2 PARP inhibitor resistance breast cancer'))\n"
    )
    steps.append(
        Step(
            idx=5,
            kind=StepKind.EXECUTE,
            code=code3,
            tools=[ToolCall(name="query_pubmed", module="biomni.tool.literature")],
            duration_ms=3100,
        )
    )
    steps.append(
        Step(
            idx=6,
            kind=StepKind.OBSERVATION,
            text=OBSERVATION_LITERATURE,
            citations=extract_citations(OBSERVATION_LITERATURE, step_idx=6),
        )
    )

    steps.append(
        Step(
            idx=7,
            kind=StepKind.SOLUTION,
            text="FGFR2 の発現上昇が PARP 阻害剤耐性に関与する可能性がある。",
        )
    )
    return steps


SAMPLE_SOLUTION = "FGFR2 の発現上昇が TNBC における PARP 阻害剤耐性に関与する可能性がある。"

SAMPLE_QUESTION = "トリプルネガティブ乳がんで PARP 阻害剤耐性を規定する因子の候補は？"


class FakeLLM:
    """invoke() が固定の応答を返すだけの LLM スタブ。

    幻覚テスト用に、存在しない eid（E999）を混ぜた応答も返せる。
    """

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[Any] = []

    def invoke(self, messages: Any) -> Any:
        self.calls.append(messages)

        class _R:
            content = self.response

        return _R()


def fake_extraction_response(*, include_unknown_eid: bool = False) -> str:
    """sample_steps() の候補 ID を前提とした、もっともらしい抽出応答。"""
    evidence = [
        {
            "eid": "E1",
            "stance": "supports",
            "claim_span": "FGFR2 の発現上昇",
            "why": "GWAS Catalog で FGFR2 座位が乳がんリスクと強く関連する",
        },
        {
            "eid": "E10",
            "stance": "supports",
            "claim_span": "PARP 阻害剤耐性への寄与",
            "why": "DepMap で乳がん系列の FGFR2 依存性が観察される",
        },
    ]
    if include_unknown_eid:
        evidence.append(
            {"eid": "E999", "stance": "supports", "claim_span": "捏造", "why": "存在しない根拠"}
        )
    payload = {
        "hypotheses": [
            {
                "statement": "FGFR2 の発現上昇が TNBC における PARP 阻害剤耐性に寄与する",
                "rationale": "GWAS で FGFR2 座位が乳がんリスクと関連し、DepMap でも乳がん系列での依存性が見られる。",
                "confidence": "medium",
                "novelty": "emerging",
                "evidence": evidence,
                "assumptions": ["FGFR2 座位のリスク変異が発現量に反映される"],
                "test_plan": {
                    "experiment": "MDA-MB-231 で FGFR2 を CRISPRi ノックダウンし、オラパリブ感受性を測定",
                    "readout": "IC50 と γH2AX フォーカス数",
                    "controls": ["非標的 sgRNA", "BRCA1 ノックダウン"],
                    "feasibility": "high",
                    "estimated_effort": "3 週間",
                },
            },
            {
                "statement": "相同組換え修復の代償経路が BRCA 非変異型 TNBC の耐性を説明する",
                "rationale": "根拠は得られていないが、機序として検討に値する。",
                "confidence": "low",
                "novelty": "speculative",
                "evidence": [],
                "assumptions": [],
                "test_plan": {"experiment": "-", "readout": "-", "controls": []},
            },
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


class _Msg:
    """LangChain の AIMessage 相当（content 属性だけ持つ）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class FakeGraph:
    """agent.app.stream() の振る舞いを再現する。

    LangGraph は stream_mode="values" で「その時点の状態全体」を返すので、
    メッセージが 1 件ずつ増えていく形で yield する。
    """

    def __init__(self, messages: list[str], exec_hook: Any = None) -> None:
        self.messages = messages
        self.exec_hook = exec_hook
        self.calls: list[Any] = []

    def stream(self, inputs: Any, stream_mode: str = "values", config: Any = None):
        self.calls.append(config)
        state: list[Any] = list(inputs.get("messages", []))
        for text in self.messages:
            if self.exec_hook is not None and "<execute>" in text:
                import re as _re

                code = _re.search(r"<execute>(.*?)</execute>", text, _re.DOTALL)
                if code:
                    self.exec_hook(code.group(1))
            state = state + [_Msg(text)]
            yield {"messages": list(state), "next_step": None}


class FakeAgent:
    """TracingRunner が触る範囲だけを実装した A1 のスタブ。"""

    def __init__(self, messages: list[str], data_lake: dict[str, str] | None = None) -> None:
        self.app = FakeGraph(messages)
        self.data_lake_dict = data_lake or {
            "gwas_catalog.pkl": "GWAS Catalog",
            "DepMap_CRISPRGeneEffect.csv": "DepMap",
        }
        self._custom_data: dict[str, Any] = {}
        self._execution_results: list[dict[str, Any]] = []
        self.log: list[Any] = []
        self._conversation_state = None
        self.critic_count = 0
        self.user_task = ""
        self.commercial_mode = True
        self.use_tool_retriever = False
        self.llm = None

    def _parse_tool_calls_with_modules(self, code: str) -> list[tuple[str, str]]:
        out = []
        for line in code.splitlines():
            m = __import__("re").match(r"\s*from\s+([\w.]+)\s+import\s+([\w,\s]+)", line)
            if m:
                module, names = m.group(1), m.group(2)
                for name in names.split(","):
                    out.append((name.strip(), module))
        return out

    def _prepare_resources_for_retrieval(self, prompt: str) -> dict[str, list[Any]]:
        return {"tools": [{"name": "query_gwas_catalog"}], "data_lake": [], "libraries": [], "know_how": []}

    def update_system_prompt_with_selected_resources(self, selected: Any) -> None:
        pass


class FakeA1Module:
    """policy_guard の差し替え対象になるダミーモジュール。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def run_python_repl(self, code: str) -> str:
        self.executed.append(code)
        return "OK"

    def run_bash_script(self, code: str) -> str:
        self.executed.append(code)
        return "OK"

    def run_r_code(self, code: str) -> str:
        self.executed.append(code)
        return "OK"


def fake_bundle(messages: list[str], settings: Any = None, policy: Any = None) -> Any:
    """TracingRunner に渡せる AgentBundle を作る。"""
    from biomni_hypo.agent_factory import AgentBundle
    from biomni_hypo.config import Settings
    from biomni_hypo.policy import ResourcePolicy

    return AgentBundle(
        agent=FakeAgent(messages),
        settings=settings or Settings(),
        policy=policy or ResourcePolicy.load(),
        biomni_version="fake",
    )


TRACE_MESSAGES = [
    "GWAS Catalog を確認します。\n<execute>\nfrom biomni.tool.database import query_gwas_catalog\n"
    "import pandas as pd\ngwas = pd.read_pickle('gwas_catalog.pkl')\nprint(query_gwas_catalog('breast carcinoma'))\n</execute>",
    "<observation>" + OBSERVATION_GWAS + "</observation>",
    "次に DepMap を見ます。\n<execute>\nimport pandas as pd\n"
    "dep = pd.read_csv('DepMap_CRISPRGeneEffect.csv')\nprint(dep.head())\n</execute>",
    "<observation>" + OBSERVATION_DEPMAP + "</observation>",
    "<solution>" + SAMPLE_SOLUTION + "</solution>",
]

#: stop シーケンスが効いていないときの出力（AC-1 の検知テスト用）
TRACE_MESSAGES_HALLUCINATED = [
    "<execute>\nprint('hi')\n</execute>\n<observation>私が勝手に書いた実行結果</observation>",
    "<solution>結論</solution>",
]

#: ポリシー違反のコードを含むトレース
TRACE_MESSAGES_POLICY = [
    "<execute>\nfrom biomni.tool.database import query_kegg\nprint(query_kegg('hsa04110'))\n</execute>",
    "<observation>POLICY BLOCKED: このコードは商用利用ポリシーに違反するため実行されませんでした。</observation>",
    "<solution>別の方法を検討します</solution>",
]
