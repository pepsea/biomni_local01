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
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any

from biomni_hypo.config import Settings, apply_biomni_env
from biomni_hypo.llm import build_agent_llm
from biomni_hypo.models import ModelNotAvailable, apply_model_selection
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
    #: LLM のトークンストリーム。ランごとに sink を差し替えて実況に使う
    token_stream: Any = None
    #: import できずに除外したモジュール -> 不足パッケージ
    unusable_modules: dict[str, str] = field(default_factory=dict)
    #: 関数内 import の依存が足りず外したツール名 -> 不足パッケージ
    unusable_tools: dict[str, str] = field(default_factory=dict)

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
            f"modules unusable : {', '.join(self.unusable_modules) or '(なし)'}",
            f"system prompt    : {self.system_prompt_chars:,} 文字 "
            f"(≒{self.estimated_prompt_tokens:,} トークン / num_ctx の {self.context_utilization:.0%})",
        ]
        if self.context_utilization > CONTEXT_WARN_RATIO:
            lines.append(
                f"  ⚠️ システムプロンプトが context の {self.context_utilization:.0%} を占めています。"
                "モジュールを減らすか num_ctx を上げてください。"
            )
        for module, package in self.unusable_modules.items():
            lines.append(f"  ⚠️ {module} を除外しました（{package} が未インストール）")
        if self.unusable_modules:
            lines.append(f"  → 入れるなら: pip install {' '.join(sorted(set(self.unusable_modules.values())))}")
        return "\n".join(lines)


def build_agent(
    settings: Settings | None = None,
    policy: ResourcePolicy | None = None,
    *,
    tool_modules: tuple[str, ...] | None = DEFAULT_TOOL_MODULES,
    download_datasets: bool = False,
    resolve_model: bool = True,
) -> AgentBundle:
    """A1 を構築して返す。

    Args:
        tool_modules: 残すモジュール。None なら絞り込みなし。
        download_datasets: True なら許可リストのデータセットを S3 から取得する。
            False（既定）だと A1 は取得をスキップし、既にディスクにあるものだけを使う。
        resolve_model: True なら Ollama に問い合わせて、モデルが実在するか・
            ライセンスが通るかを確認し、num_ctx をモデルの上限に丸める。
    """
    settings = settings or Settings()
    policy = policy or ResourcePolicy.load(settings.policy_path)

    if resolve_model:
        # ローカルのモデルを読み、選択・ライセンス判定・num_ctx の丸めをまとめて行う
        catalog, notes = apply_model_selection(settings, policy)
        for note in notes:
            log.warning("%s", note)
    else:
        decision = policy.check_model(settings.model)
        if not decision.allowed:
            raise ModelNotAvailable(
                f"モデル {settings.model!r} は商用利用ポリシーにより使用できません: {decision.reason}"
            )

    # ★ biomni の import より前に環境変数を入れる（§4.3）。順序を入れ替えないこと。
    # モデル名が確定してから呼ぶこと（default_config.llm にこの名前が入る）。
    applied = apply_biomni_env(settings)
    log.info("biomni env applied: %s", applied)

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
    # 同時にトークンストリームのハンドラを仕込む（リアルタイム表示用）。
    agent.llm, token_stream = build_agent_llm(settings)

    kept_modules = _restrict_modules(agent, tool_modules)
    # import できないモジュールのツールは、案内しても実行時に必ず失敗する。
    # 案内すると「ツールを呼ぶ -> ImportError -> 直そうとする」のループに陥るので、
    # 先に落としておく（実運用で観測した最頻の失敗モード）。
    patch_biomni_get_llm(settings, policy)
    unusable = _drop_unimportable_modules(agent)
    kept_modules = [m for m in kept_modules if m not in unusable]
    unusable_tools = _drop_tools_with_missing_lazy_imports(agent)
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
        token_stream=token_stream,
        unusable_modules=unusable,
        unusable_tools=unusable_tools,
    )
    if not agent.module2api:
        raise RuntimeError(
            "使えるツールが 1 つもありません。ツールモジュールの依存が不足しています:\n  pip install "
            + " ".join(sorted(set(unusable.values())))
        )
    if unusable:
        log.warning(
            "import できないモジュールを除外しました: %s。入れるなら pip install %s",
            ", ".join(unusable),
            " ".join(sorted(set(unusable.values()))),
        )
    if unusable_tools:
        log.warning(
            "関数内 import の依存が足りないツールを除外しました: %s。"
            "入れるなら pip install %s",
            ", ".join(sorted(unusable_tools)),
            " ".join(sorted(set(unusable_tools.values()))),
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


#: パッチが使う現在の settings。パッチ自体は 1 プロセス 1 回しか当たらないが、
#: ランごとに設定は変わりうるので、こちらは毎回更新する
_PATCH_SETTINGS: Settings | None = None


def patch_biomni_get_llm(settings: Settings, policy: ResourcePolicy) -> bool:  # noqa: ARG001
    """biomni.llm.get_llm が Claude に temperature を送らないようにする。

    `biomni/tool/database.py::_query_llm_for_api()` は A1 のコンストラクタ引数を
    見ず、`get_llm(model=..., temperature=0.0, config=default_config)` を自分で
    呼ぶ。そして biomni の Anthropic 分岐は temperature を素通しする
    （`biomni/llm.py:166`）。

        return ChatAnthropic(model=model, temperature=temperature, ...)

    Claude 4.6 以降（Opus 5 / Sonnet 5 / Opus 4.8 …）は temperature を受け付けず
    400 を返すので、query_opentarget などの DB ツールが軒並み落ちる。実測:

        {'success': False, 'error': "... 400 ... '`temperature` is deprecated
         for this model.'"}

    agent.llm を差し替えるだけでは、この経路は直らない（§4.3 と同じ構造の問題で、
    強制ポイントが 1 つ増えたということ）。

    Returns:
        パッチを当てたら True。
    """
    try:
        import biomni.llm as biomni_llm
    except Exception:  # noqa: BLE001 - biomni が無い環境では何もしない
        return False

    # settings をクロージャに閉じ込めない。パッチは 1 プロセス 1 回しか当たらない
    # ので、閉じ込めると **最初のランの settings が居座る**。プロバイダやキーを
    # 変えた 2 回目以降のランで、古いキーを使う / キーが無いと誤判定する。
    global _PATCH_SETTINGS
    _PATCH_SETTINGS = settings
    if getattr(biomni_llm.get_llm, "_hypo_patched", False):
        return True

    original = biomni_llm.get_llm

    def get_llm(*args: Any, **kwargs: Any) -> Any:
        model, source = _resolve_model_and_source(args, kwargs)
        current = _PATCH_SETTINGS or settings

        # DB ツール（query_uniprot など）の「自然文 → API の URL」だけ、
        # 別のモデルに寄せられるようにする。ここはスキーマ厳守が要る一方、
        # エージェント本体はローカルのままでよい（docs/design/24）。
        wanted = current.tool_query_model_name
        if wanted and wanted != model:
            log.debug("DB ツールの URL 生成に %s を使います（本体は %s）", wanted, model)
            model = wanted
            source = "Anthropic" if model.startswith("claude") else source

        if source != "Anthropic":
            if wanted and wanted != _resolve_model_and_source(args, kwargs)[0]:
                kwargs = {**kwargs, "model": model}
            return original(*args, **kwargs)

        # temperature を kwargs から消すだけでは直らない。biomni の get_llm は
        #
        #     if temperature is None: temperature = config.temperature   # 0.7
        #     if temperature is None: temperature = 0.7
        #
        # と**自分で埋め直して** ChatAnthropic に渡す（biomni/llm.py:38,52）。
        # 消しても 0.7 が入るだけで、400 は消えない。
        #
        # なので biomni に作らせない。Anthropic のときは自前で組む。
        # build_chat_anthropic() は temperature を既定で送らない（§4.1）。
        from biomni_hypo.llm import build_chat_anthropic

        stop = kwargs.get("stop_sequences")
        if not stop and len(args) >= 3:
            stop = args[2]
        try:
            return build_chat_anthropic(
                current, model=model, stop=stop, temperature=None, streaming=False
            )
        except Exception as exc:  # noqa: BLE001 - 作れなければ biomni に任せる
            log.warning("Anthropic クライアントを自前で作れませんでした: %s", exc)
            return original(*args, **kwargs)

    get_llm._hypo_patched = True  # type: ignore[attr-defined]
    biomni_llm.get_llm = get_llm

    # `from biomni.llm import get_llm` で取り込んだモジュールは、**自分の名前空間に
    # 元の関数オブジェクトを束縛している**。biomni.llm を差し替えても、そちらは
    # 古いままになる。biomni 0.0.8 で先頭 import しているのは 5 つ:
    #   agent/a1.py, agent/react.py, agent/qa_llm.py,
    #   agent/function_generator.py, tool/database.py, tool/genomics.py
    # 名前を並べると、biomni の更新で増えたときに漏れる。
    # 「元の関数を握っているモジュール」を実際に探して差し替える。
    patched = 0
    for module in list(sys.modules.values()):
        if module is None or module is biomni_llm:
            continue
        if getattr(module, "get_llm", None) is original:
            module.get_llm = get_llm
            patched += 1
    log.debug("get_llm を差し替えたモジュール: %d 件", patched)
    return True


def _resolve_model_and_source(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str]:
    """biomni の get_llm と同じ順で model と source を決める。

    biomni/llm.py の先頭と同じ規則:
      1. 引数
      2. config（BiomniConfig）
      3. 既定値 / モデル名からの推定
    ここがずれると、Anthropic なのに素通しして 400 に戻る。
    """
    model = kwargs.get("model") or (args[0] if args else None)
    source = kwargs.get("source") or (args[3] if len(args) >= 4 else None)
    config = kwargs.get("config") or (args[6] if len(args) >= 7 else None)

    if config is not None:
        if model is None:
            model = getattr(config, "llm_model", None) or getattr(config, "llm", None)
        if source is None:
            source = getattr(config, "source", None)
    if model is None:
        model = "claude-3-5-sonnet-20241022"      # biomni の既定
    if not source:
        # biomni はモデル名から推定する。claude で始まれば Anthropic
        source = "Anthropic" if str(model).startswith("claude") else ""
    return str(model), str(source)


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


#: import 失敗時に出てくるモジュール名 -> 入れるべき pip パッケージ名
_PACKAGE_FOR_MODULE = {
    "Bio": "biopython",
    "bs4": "beautifulsoup4",
    "PyPDF2": "PyPDF2",
    "googlesearch": "googlesearch-python",
    "torch": "torch",
    "esm": "fair-esm",
    "rdkit": "rdkit",
    "scanpy": "scanpy",
    "anndata": "anndata",
    "pyensembl": "pyensembl",
}


def _drop_unimportable_modules(agent: Any) -> dict[str, str]:
    """実際に import できないツールモジュールを外す。

    biomni のツールモジュールはそれぞれ独自の依存を持つ（database は Biopython、
    literature は BeautifulSoup など）。入っていないと、エージェントは
    システムプロンプトで案内されたツールを呼び、ImportError を受け取り、
    直そうとして同じことを繰り返す。実運用で観測した最頻の失敗モード。

    Returns:
        除外したモジュール名 -> 不足パッケージ名
    """
    import importlib

    unusable: dict[str, str] = {}
    for module in list(agent.module2api):
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - どんな失敗でも「使えない」で同じ
            missing = getattr(exc, "name", None) or type(exc).__name__
            unusable[module] = _PACKAGE_FOR_MODULE.get(missing, missing)
            del agent.module2api[module]

    registry = getattr(agent, "tool_registry", None)
    if registry is not None and unusable:
        usable_names = {a["name"] for apis in agent.module2api.values() for a in apis}
        for tool in list(getattr(registry, "tools", [])):
            if tool.get("name") not in usable_names:
                registry.remove_tool_by_name(tool["name"])
    return unusable


def _drop_tools_with_missing_lazy_imports(agent: Any) -> dict[str, str]:
    """関数の中で import しているツールを、その依存が無ければ外す。

    biomni のツールは依存を**関数の中で** import することがある:

        def query_pubmed(query, ...):
            from pymed import PubMed      # ← モジュールを import しても通る

    そのためモジュール単位の検査（_drop_unimportable_modules）を素通りし、
    エージェントが呼んだ瞬間に ModuleNotFoundError になる。実測:

        [ 2] execute     query_pubmed(...)
        [ 3] observation Error: No module named 'pymed'
        …
        [34] think       Let me compile the research manually based on ...
                         what I've found and my knowledge base

    最後の行が本当の被害。文献を引けなかったエージェントは**自分の記憶で
    書き始める**ので、根拠を示すという前提そのものが崩れる（docs/design/20）。
    呼べないツールは最初から見せない。

    Returns:
        外したツール名 -> 不足パッケージ名
    """
    import ast
    import importlib.util
    import inspect

    dropped: dict[str, str] = {}
    cache: dict[str, bool] = {}

    def available(name: str) -> bool:
        root = name.split(".")[0]
        if root not in cache:
            try:
                cache[root] = importlib.util.find_spec(root) is not None
            except (ImportError, ValueError):
                cache[root] = False
        return cache[root]

    for module, apis in list(agent.module2api.items()):
        try:
            mod = importlib.import_module(module)
        except Exception:  # noqa: BLE001 - モジュール単位の検査で扱う
            continue
        kept = []
        for api in apis:
            name = api.get("name", "")
            fn = getattr(mod, name, None)
            missing = _missing_lazy_imports(fn, available, ast, inspect)
            if missing:
                dropped[name] = _PACKAGE_FOR_MODULE.get(missing[0], missing[0])
            else:
                kept.append(api)
        if len(kept) != len(apis):
            agent.module2api[module] = kept

    registry = getattr(agent, "tool_registry", None)
    if registry is not None:
        for name in dropped:
            if registry.get_tool_by_name(name) is not None:
                registry.remove_tool_by_name(name)
    return dropped


def _missing_lazy_imports(fn: Any, available: Any, ast: Any, inspect: Any) -> list[str]:
    """関数本体の import 文を読み、入っていないものを返す。

    実行はしない。ソースを AST で読むだけなので副作用が無い。
    """
    if fn is None or not callable(fn):
        return []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return []
    # try: の中の import は、作者が不在を織り込んでいる（代替に落ちる）。
    # これを理由にツールごと外すと、動くはずのものまで消える
    guarded = {
        id(n)
        for t in ast.walk(tree)
        if isinstance(t, ast.Try)
        for n in ast.walk(t)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    missing: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` のような相対 import は対象外
            names = [node.module] if node.module and node.level == 0 else []
        else:
            continue
        for candidate in names:
            if candidate and not available(candidate):
                missing.append(candidate.split(".")[0])
    return missing


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
