"""依存チェック.

`import biomni` が通るだけでは足りない。biomni 0.0.8 の pyproject は
pydantic / langchain / python-dotenv しか宣言しておらず、pandas と
langchain-openai が無いと A1 を import できない。Ollama を使うには
langchain-ollama も要る（これが無いと notebook 01 で初めて失敗する）。
"""

from biomni_hypo.config import (
    AGENT_DEPENDENCIES,
    API_DEPENDENCIES,
    Dependency,
    install_hint,
    missing_dependencies,
)


def test_agent_dependencies_cover_biomnis_undeclared_ones():
    modules = {d.module for d in AGENT_DEPENDENCIES}
    assert {"biomni", "langchain_ollama", "pandas", "langchain_openai"} <= modules


def test_every_dependency_says_why_it_is_needed():
    for d in AGENT_DEPENDENCIES + API_DEPENDENCIES:
        assert d.why, f"{d.module} に理由が書かれていない"


def test_installed_uses_find_spec_and_does_not_import():
    """find_spec なので、重い biomni を読み込まずに調べられる。"""
    assert Dependency("json", "json", "標準ライブラリ").installed
    assert not Dependency("definitely_not_a_real_module_xyz", "x", "y").installed


def test_missing_dependencies_returns_only_missing():
    group = (
        Dependency("json", "json", "ある"),
        Dependency("definitely_not_a_real_module_xyz", "ghost-pkg", "ない"),
    )
    missing = missing_dependencies(group)
    assert [d.module for d in missing] == ["definitely_not_a_real_module_xyz"]


def test_install_hint_uses_pip_names_not_module_names():
    """import 名と pip 名は食い違う（langchain_ollama / langchain-ollama）。"""
    missing = [Dependency("langchain_ollama", "langchain-ollama", "x")]
    assert install_hint(missing) == "pip install langchain-ollama"
    assert install_hint([]) == ""


def test_install_hint_deduplicates_and_sorts():
    missing = [
        Dependency("a", "pkg-b", "x"),
        Dependency("b", "pkg-a", "y"),
        Dependency("c", "pkg-a", "z"),
    ]
    assert install_hint(missing) == "pip install pkg-a pkg-b"


# ------------------------------------------------- 関数内 import（遅延 import）
# biomni のツールは依存を関数の中で import することがあり、モジュール単位の
# 検査を素通りする。実測: query_pubmed が "No module named 'pymed'" で落ち、
# 文献を引けなくなったエージェントが自分の記憶で書き始めた（docs/design/20）。


def test_lazy_imports_are_detected():
    import ast
    import importlib
    import inspect

    from biomni_hypo.agent_factory import _missing_lazy_imports

    mod = importlib.import_module("biomni.tool.literature")

    def missing_everything(_name: str) -> bool:
        return False

    found = _missing_lazy_imports(
        mod.query_pubmed, missing_everything, ast, inspect
    )
    assert "pymed" in found, "query_pubmed の関数内 import を見落としている"


def test_lazy_imports_are_satisfied_in_this_environment():
    """query_pubmed / query_arxiv が実際に呼べる状態であること。

    ここが赤いと、エージェントは文献を引けずに自分の記憶で書き始める。
    """
    import importlib

    for module in ("pymed", "arxiv"):
        importlib.import_module(module)


def test_pubmed_and_arxiv_are_declared():
    from biomni_hypo.config import TOOL_DEPENDENCIES

    declared = {d.module for d in TOOL_DEPENDENCIES}
    assert {"pymed", "arxiv"} <= declared


def test_biomni_is_pinned():
    """検証済みバージョンから静かにずれないこと（docs/design/04 は 0.0.8 実測）。"""
    from pathlib import Path

    req = Path(__file__).resolve().parents[1] / "requirements.txt"
    assert "biomni==0.0.8" in req.read_text(encoding="utf-8")
