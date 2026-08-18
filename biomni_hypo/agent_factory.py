"""Biomni A1 エージェントの構築.

docs/design/04-ollama-integration.md で洗い出した落とし穴を、すべてこの 1 ファイルに封じ込める。
A1 を直接 new する箇所を他に作らないこと。

封じている問題:
  §4.1 Ollama 分岐で stop シーケンスが渡らない -> agent.llm を差し替え
  §4.2 base_url が Ollama に渡らない           -> 同上
  §4.3 database.py が default_config を見る     -> apply_biomni_env() を import 前に実行
  §4.4 データレイクの一括ダウンロード           -> expected_data_lake_files を明示
  §4.5 リソース検索プロンプトの肥大             -> モジュール絞り込み + 既定 False
  §5.2 commercial_mode がツールを絞らない       -> tool_registry / module2api から除去
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from biomni_hypo.config import Settings, apply_biomni_env
from biomni_hypo.llm import build_agent_llm
from biomni_hypo.policy import ResourcePolicy

log = logging.getLogger(__name__)

# --- ツールモジュールのプリセット（§4.5） -------------------------------------
#
# biomni 0.0.8 で実測したシステムプロンプトのサイズ（commercial_mode=True）:
#
#   絞り込みなし (21 モジュール / 214 ツール) : 154,296 文字 ≒ 38.6k トークン
#   EXTENDED     (11 モジュール / 144 ツール) : 107,391 文字 ≒ 26.8k トークン
#   DEFAULT      ( 5 モジュール /  75 ツール) :  66,008 文字 ≒ 16.5k トークン
#   CORE         ( 3 モジュール /  47 ツール) :  38,115 文字 ≒  9.5k トークン
#
# 絞り込まないと **システムプロンプトだけで num_ctx=32768 を超える**。
# 会話が 1 往復も入らないので、既定は DEFAULT にしてある。

#: 文献検索と公共 DB だけ。動作確認や、軽いモデルで回すとき用
CORE_TOOL_MODULES = (
    "biomni.tool.support_tools",
    "biomni.tool.literature",
    "biomni.tool.database",
)

#: 既定。仮説構築に効くゲノム・遺伝の解析を足して、なお num_ctx の半分に収まる
DEFAULT_TOOL_MODULES = CORE_TOOL_MODULES + (
    "biomni.tool.genomics",
    "biomni.tool.genetics",
)

#: 広く使いたい場合。num_ctx を 65536 以上にしてから使うこと
EXTENDED_TOOL_MODULES = DEFAULT_TOOL_MODULES + (
    "biomni.tool.molecular_biology",
    "biomni.tool.cell_biology",
    "biomni.tool.cancer_biology",
    "biomni.tool.pharmacology",
    "biomni.tool.systems_biology",
    "biomni.tool.immunology",
)

#: 英数主体のプロンプトのトークン数のざっくり換算（1 トークン ≒ 4 文字）
CHARS_PER_TOKEN = 4

#: システムプロンプトが num_ctx のこの割合を超えたら警告する
CONTEXT_WARN_RATIO = 0.4


@dataclass
class AgentBundle:
    """A1 と、その構築時に適用した制約をまとめて持つ。

    ノートブックでは bundle.report を print すれば、何が効いているか一目で分かる。
    """

    agent: Any
    settings: Settings
    policy: ResourcePolicy
    removed_tools: list[str] = field(default_factory=list)
    kept_modules: list[str] = field(default_factory=list)
    tool_count: int = 0
    biomni_version: str = ""
    system_prompt_chars: int = 0

    @property
    def estimated_prompt_tokens(self) -> int:
        return self.system_prompt_chars // CHARS_PER_TOKEN

    @property
    def context_utilization(self) -> float:
        """システムプロンプトが num_ctx に占める割合。

        これが 1.0 を超えると会話が 1 往復も入らない。0.4 を超えたら
        モジュールを減らすか num_ctx を上げること。
        """
        return self.estimated_prompt_tokens / max(1, self.settings.num_ctx)

    @property
    def report(self) -> str:
        lines = [
            "=== AgentBundle ===",
            f"model            : {self.settings.model} (Ollama @ {self.settings.ollama_base_url})",
            f"num_ctx          : {self.settings.num_ctx}",
            f"stop sequences   : {getattr(self.agent.llm, 'stop', None)}",
            f"biomni           : {self.biomni_version}",
            f"commercial_mode  : {getattr(self.agent, 'commercial_mode', None)}",
            f"use_tool_retriever: {getattr(self.agent, 'use_tool_retriever', None)}",
            f"policy version   : {self.policy.version}",
            f"tools available  : {self.tool_count}",
            f"tools removed    : {', '.join(self.removed_tools) or '(なし)'}",
            f"modules kept     : {len(self.kept_modules)}",
            f"system prompt    : {self.system_prompt_chars:,} 文字 "
            f"(≒{self.estimated_prompt_tokens:,} トークン / num_ctx の {self.context_utilization:.0%})",
        ]
        if self.context_utilization > CONTEXT_WARN_RATIO:
            lines.append(
                f"  ⚠️ システムプロンプトが context の {self.context_utilization:.0%} を占めています。"
                "モジュールを減らすか num_ctx を上げてください。"
            )
        return "\n".join(lines)


def build_agent(
    settings: Settings | None = None,
    policy: ResourcePolicy | None = None,
    *,
    tool_modules: tuple[str, ...] | None = DEFAULT_TOOL_MODULES,
    download_datasets: bool = False,
) -> AgentBundle:
    """A1 を構築して返す。

    Args:
        tool_modules: 残すモジュール。None なら絞り込みなし。
        download_datasets: True なら許可リストのデータセットを S3 から取得する。
            False（既定）だと A1 は取得をスキップし、既にディスクにあるものだけを使う。
    """
    settings = settings or Settings()
    policy = policy or ResourcePolicy.load(settings.policy_path)

    # ★ biomni の import より前に環境変数を入れる（§4.3）。順序を入れ替えないこと。
    applied = apply_biomni_env(settings)
    log.info("biomni env applied: %s", applied)

    model_decision = policy.check_model(settings.model)
    if not model_decision.allowed:
        raise ValueError(
            f"モデル {settings.model!r} は商用利用ポリシーにより使用できません: {model_decision.reason}"
        )

    from biomni.agent import A1  # 遅延 import（環境変数適用後）
    from biomni.version import __version__ as biomni_version

    # expected_data_lake_files を必ず渡す（§4.4）。
    # 空リストを渡すと A1 はダウンロードを完全にスキップする。
    expected = policy.allowed_dataset_names() if download_datasets else []

    agent = A1(
        path=settings.data_path,
        llm=settings.model,
        source="Ollama",
        use_tool_retriever=settings.use_tool_retriever,
        timeout_seconds=settings.timeout_seconds,
        commercial_mode=True,  # 常に True（docs/design/05）
        expected_data_lake_files=expected,
    )

    # ★ stop シーケンスと num_ctx を効かせるため LLM を差し替える（§4.1, §4.2）。
    # generate ノードは self.llm を実行時参照するので、グラフの再構築は不要。
    agent.llm = build_agent_llm(settings)

    kept_modules = _restrict_modules(agent, tool_modules)
    removed = _apply_tool_policy(agent, policy)

    # module2api を変更したので、システムプロンプトを作り直す。
    agent.configure()

    tool_count = sum(len(v) for v in agent.module2api.values())
    bundle = AgentBundle(
        agent=agent,
        settings=settings,
        policy=policy,
        removed_tools=removed,
        kept_modules=kept_modules,
        tool_count=tool_count,
        biomni_version=biomni_version,
        system_prompt_chars=len(getattr(agent, "system_prompt", "") or ""),
    )
    if bundle.context_utilization > CONTEXT_WARN_RATIO:
        log.warning(
            "システムプロンプトが num_ctx の %.0f%% を占めています "
            "(%s トークン / num_ctx=%s)。モジュールを減らすか num_ctx を上げてください。",
            bundle.context_utilization * 100,
            bundle.estimated_prompt_tokens,
            settings.num_ctx,
        )
    log.info("agent built: %s tools, %s removed", tool_count, len(removed))
    return bundle


def _restrict_modules(agent: Any, tool_modules: tuple[str, ...] | None) -> list[str]:
    """module2api を指定モジュールだけに絞る（§4.5）。"""
    if tool_modules is None:
        return list(agent.module2api)
    keep = {m for m in agent.module2api if m in tool_modules}
    if not keep:
        log.warning("指定モジュールが 1 つも一致しませんでした。絞り込みをスキップします。")
        return list(agent.module2api)
    agent.module2api = {m: agent.module2api[m] for m in keep}
    return sorted(keep)


def _apply_tool_policy(agent: Any, policy: ResourcePolicy) -> list[str]:
    """拒否ツールを module2api と tool_registry の両方から取り除く（§5.2 強制ポイント 2）。

    module2api を消すだけではリソース検索が拾ってしまい、
    tool_registry を消すだけではシステムプロンプトに載り続ける。両方消す必要がある。
    """
    denied = set(policy.denied_tool_names())
    removed: list[str] = []

    for module, apis in list(agent.module2api.items()):
        kept = [a for a in apis if a.get("name") not in denied]
        if len(kept) != len(apis):
            removed += [a["name"] for a in apis if a.get("name") in denied]
            agent.module2api[module] = kept

    registry = getattr(agent, "tool_registry", None)
    if registry is not None:
        for name in denied:
            if registry.get_tool_by_name(name) is not None:
                registry.remove_tool_by_name(name)
                if name not in removed:
                    removed.append(name)

    return sorted(set(removed))


def reset_agent_state(agent: Any) -> None:
    """ラン間でエージェントの内部状態を消す（docs/design/02 §2.5）。

    A1 はステートフルで、log / _execution_results / _conversation_state を溜め込む。
    使い回すならラン開始前に必ず呼ぶこと。
    """
    agent.log = []
    agent._execution_results = []
    agent._conversation_state = None
    agent.critic_count = 0
    try:
        from biomni.tool.support_tools import clear_captured_plots

        clear_captured_plots()
    except Exception:  # noqa: BLE001 - バージョン差異は無視してよい
        pass
