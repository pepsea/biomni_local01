"""設定. 環境変数 -> Settings -> RunConfig の一方向で流す.

重要: biomni をインポートする *前に* apply_biomni_env() を呼ぶこと。
biomni.config.default_config はモジュール読み込み時に環境変数を読むため、
後から設定しても DB クエリツールが Anthropic を呼びに行く
（docs/design/04-ollama-integration.md §4.3）。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from biomni_hypo.schemas import RunConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Dependency:
    """import 名と pip 名。両者はしばしば食い違う（langchain_ollama / langchain-ollama）。"""

    module: str
    package: str
    why: str

    @property
    def installed(self) -> bool:
        # find_spec はモジュールを実行しないので、重い biomni でも安全に調べられる
        try:
            return importlib.util.find_spec(self.module) is not None
        except (ImportError, ValueError):
            return False


#: エージェントを動かすのに要るもの。
#: biomni 0.0.8 の pyproject は pydantic / langchain / python-dotenv しか宣言しておらず、
#: pandas と langchain-openai が無いと `from biomni.agent import A1` の時点で落ちる。
AGENT_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("biomni", "biomni", "エージェント本体"),
    Dependency("langchain_ollama", "langchain-ollama", "Ollama への接続（ChatOllama）"),
    Dependency("langchain_core", "langchain-core", "メッセージ型"),
    Dependency("langgraph", "langgraph", "A1 の実行グラフ"),
    Dependency("pandas", "pandas", "biomni が宣言していない実依存"),
    Dependency("langchain_openai", "langchain-openai", "biomni が宣言していない実依存"),
    Dependency("dotenv", "python-dotenv", "biomni の .env 読み込み"),
)

#: API サーバを動かすのに要るもの
API_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("fastapi", "fastapi", "API サーバ"),
    Dependency("uvicorn", "uvicorn[standard]", "ASGI サーバ"),
)

#: biomni のツールモジュールが必要とするもの。
#: **これが無いと、エージェントはツールを案内されるのに実行時に必ず失敗する。**
#: 実測（biomni 0.0.8）では、これらが無いと literature / database /
#: molecular_biology が丸ごと import できず、query_pubmed すら呼べない。
TOOL_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("Bio", "biopython", "database / molecular_biology（配列・BLAST 系）"),
    Dependency("bs4", "beautifulsoup4", "literature（HTML 解析）"),
    Dependency("PyPDF2", "PyPDF2", "literature（PDF 抽出）"),
    Dependency("googlesearch", "googlesearch-python", "literature（Web 検索。ポリシーでは拒否ツール）"),
)

#: 重いので既定では入れないもの。使いたい人向けに理由を明示する
OPTIONAL_TOOL_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("torch", "torch", "genetics モジュール（数 GB）"),
    Dependency("esm", "fair-esm", "genomics モジュール（タンパク質言語モデル）"),
)


def missing_dependencies(
    group: tuple[Dependency, ...] = AGENT_DEPENDENCIES,
) -> list[Dependency]:
    """足りない依存を返す。空なら揃っている。"""
    return [d for d in group if not d.installed]


def install_hint(missing: list[Dependency]) -> str:
    """不足分をまとめて入れるコマンド。"""
    if not missing:
        return ""
    return "pip install " + " ".join(sorted({d.package for d in missing}))


#: 使える LLM プロバイダ
PROVIDERS = ("ollama", "anthropic")


class Settings(BaseModel):
    """プロセス全体の設定。ノートブックでも Web アプリでも同じものを使う。"""

    #: "ollama"（ローカル完結）か "anthropic"（Claude API）。
    #: anthropic を選ぶと質問文と実行結果が外部に出る。UI で明示すること。
    provider: str = Field(default_factory=lambda: _env("HYPO_PROVIDER", "ollama"))
    model: str = Field(default_factory=lambda: _env("HYPO_MODEL", "qwen3:14b"))
    extractor_model: str = Field(default_factory=lambda: _env("HYPO_EXTRACTOR_MODEL", ""))
    ollama_base_url: str = Field(
        default_factory=lambda: _env("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    temperature: float = Field(default_factory=lambda: float(_env("HYPO_TEMPERATURE", "0.7")))
    extractor_temperature: float = Field(
        default_factory=lambda: float(_env("HYPO_EXTRACTOR_TEMPERATURE", "0.2"))
    )
    num_ctx: int = Field(default_factory=lambda: int(_env("HYPO_NUM_CTX", "32768")))
    num_predict: int = Field(default_factory=lambda: int(_env("HYPO_NUM_PREDICT", "4096")))
    data_path: str = Field(default_factory=lambda: _env("BIOMNI_PATH", str(REPO_ROOT / "data")))
    workspace_path: str = Field(
        default_factory=lambda: _env("HYPO_WORKSPACE", str(REPO_ROOT / "workspace"))
    )
    policy_path: str = Field(
        default_factory=lambda: _env("HYPO_POLICY_PATH", str(REPO_ROOT / "config" / "resource_policy.yaml"))
    )
    timeout_seconds: int = Field(default_factory=lambda: int(_env("BIOMNI_TIMEOUT_SECONDS", "600")))
    max_steps: int = Field(default_factory=lambda: int(_env("HYPO_MAX_STEPS", "60")))
    wallclock_limit_sec: int = Field(
        default_factory=lambda: int(_env("HYPO_WALLCLOCK_LIMIT_SEC", "1800"))
    )
    max_hypotheses: int = Field(default_factory=lambda: int(_env("HYPO_MAX_HYPOTHESES", "5")))
    #: エージェントへの指示文の言語。"en" | "ja"
    #: A1 のシステムプロンプトもツール説明も英語なので、既定は en のほうが追従が安定する。
    #: ユーザーの記述自体は翻訳せずそのまま埋め込む。
    prompt_language: str = Field(default_factory=lambda: _env("HYPO_PROMPT_LANGUAGE", "en"))
    use_tool_retriever: bool = Field(
        default_factory=lambda: _env("HYPO_USE_TOOL_RETRIEVER", "false").lower() == "true"
    )
    offline_mode: bool = Field(
        default_factory=lambda: _env("HYPO_OFFLINE_MODE", "false").lower() == "true"
    )
    #: Claude API 用。未設定なら Claude はモデル一覧に出ない
    anthropic_api_key: str = Field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    anthropic_base_url: str = Field(default_factory=lambda: _env("ANTHROPIC_BASE_URL", ""))
    anthropic_max_tokens: int = Field(
        default_factory=lambda: int(_env("HYPO_ANTHROPIC_MAX_TOKENS", "8192"))
    )
    #: 常に True。商用限定の前提を設定で緩められないようにする（docs/design/05）
    commercial_mode: bool = True

    @property
    def is_local(self) -> bool:
        """データがローカルから出ないか。"""
        return self.provider == "ollama"

    def extractor_model_name(self) -> str:
        return self.extractor_model or self.model

    def to_run_config(self, policy_version: int = 0, biomni_version: str = "") -> RunConfig:
        return RunConfig(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            num_ctx=self.num_ctx,
            num_predict=self.num_predict,
            ollama_base_url=self.ollama_base_url,
            data_path=self.data_path,
            timeout_seconds=self.timeout_seconds,
            max_steps=self.max_steps,
            wallclock_limit_sec=self.wallclock_limit_sec,
            max_hypotheses=self.max_hypotheses,
            use_tool_retriever=self.use_tool_retriever,
            commercial_mode=self.commercial_mode,
            offline_mode=self.offline_mode,
            policy_version=policy_version,
            biomni_version=biomni_version,
        )


def apply_biomni_env(settings: Settings) -> dict[str, str]:
    """biomni の default_config を Ollama に向ける環境変数を設定する.

    biomni/tool/database.py::_query_llm_for_api() は A1 のコンストラクタ引数ではなく
    biomni.config.default_config を見るため、これを設定しないと
    query_uniprot / query_ensembl などを呼んだ瞬間に Anthropic API を叩きに行く。

    Returns:
        設定した環境変数（検証・ログ用）。
    """
    source = "Anthropic" if settings.provider == "anthropic" else "Ollama"
    env = {
        "BIOMNI_LLM": settings.model,
        "BIOMNI_SOURCE": source,
        "LLM_SOURCE": source,
        "BIOMNI_PATH": settings.data_path,
        "BIOMNI_TIMEOUT_SECONDS": str(settings.timeout_seconds),
        "BIOMNI_COMMERCIAL_MODE": "true",
        "BIOMNI_TEMPERATURE": str(settings.temperature),
        "OLLAMA_HOST": settings.ollama_base_url,
    }
    os.environ.update(env)
    return env


def assert_biomni_env(settings: Settings) -> None:
    """biomni インポート後に、default_config が実際に Ollama を向いているか確認する.

    ノートブック 00 と ワーカー起動時の両方で呼ぶ。
    """
    from biomni.config import default_config  # 遅延 import

    expected_source = "Anthropic" if settings.provider == "anthropic" else "Ollama"
    problems = []
    if default_config.llm != settings.model:
        problems.append(f"default_config.llm={default_config.llm!r} (期待: {settings.model!r})")
    if default_config.source != expected_source:
        problems.append(
            f"default_config.source={default_config.source!r} (期待: {expected_source!r})"
        )
    if not default_config.commercial_mode:
        problems.append("default_config.commercial_mode=False (期待: True)")
    if problems:
        raise RuntimeError(
            "biomni の default_config が想定どおりを向いていません。"
            "biomni を import する前に apply_biomni_env() を呼んでください。\n  - "
            + "\n  - ".join(problems)
        )
