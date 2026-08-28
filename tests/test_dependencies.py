"""依存チェック.

`import biomni` が通るだけでは足りない。biomni 0.0.8 の pyproject は
pydantic / langchain / python-dotenv しか宣言しておらず、pandas と
langchain-openai が無いと A1 を import できない。Ollama を使うには
langchain-ollama も要る（これが無いと notebook 01 で初めて失敗する）。
"""

from pathlib import Path

import pytest

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

    pytest.importorskip("biomni", reason="最小構成では biomni を入れない")
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

    # 最小構成（bash scripts/setup_local.sh）では biomni ごと入れないので、
    # ツールの依存が無いのは当然。**エージェントを入れたのに**ツールの依存だけ
    # 欠けている状態を捕まえたいので、biomni の有無で切り替える
    pytest.importorskip("biomni", reason="最小構成では biomni を入れない")

    missing = []
    for module in ("pymed", "arxiv"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    assert not missing, (
        f"{', '.join(missing)} が入っていません。requirements.txt に追加された依存です。\n"
        "  直す:  pip install -r requirements.txt\n"
        "         （仮想環境を使っているなら .venv/bin/pip install -r requirements.txt）\n"
        "  入れないと query_pubmed / query_arxiv が呼ばれた瞬間に落ち、\n"
        "  文献を引けないエージェントが自分の記憶で書き始めます（docs/design/20）。"
    )


def test_pubmed_and_arxiv_are_declared():
    from biomni_hypo.config import TOOL_DEPENDENCIES

    declared = {d.module for d in TOOL_DEPENDENCIES}
    assert {"pymed", "arxiv"} <= declared


def test_biomni_is_pinned():
    """検証済みバージョンから静かにずれないこと（docs/design/04 は 0.0.8 実測）。"""
    from pathlib import Path

    req = Path(__file__).resolve().parents[1] / "requirements.txt"
    assert "biomni==0.0.8" in req.read_text(encoding="utf-8")


# ------------------------------------------- 最小構成でテストが通ること
# `bash scripts/setup_local.sh`（--full 無し）は「テストが通るところまで」を
# 謳っている。ところがテスト側が langchain や biomni をモジュール先頭で
# import すると、**collection の時点で全体が止まる**。1 ファイルの import 文が
# テスト全体を殺すので、件数ではなく構造で防ぐ。実測で踏んだ:
#
#   ERROR tests/test_api.py            (fastapi)
#   ERROR tests/test_format_reminder.py (langchain_core)
#   Interrupted: 2 errors during collection

#: 最小構成には入らないもの。テストのモジュール先頭で import してはいけない
OPTIONAL_AT_IMPORT_TIME = (
    "biomni",
    "langchain",
    "langchain_core",
    "langchain_ollama",
    "langchain_openai",
    "langchain_anthropic",
    "langgraph",
    "pandas",
    "pymed",
    "arxiv",
    "Bio",
    "bs4",
)


def _module_level_imports(source: str) -> set[str]:
    """モジュール先頭で無条件に import しているものを返す。

    先に `pytest.importorskip("X")` を置いてある X は、無い環境では
    そこで skip されるので数えない（それが正しい書き方）。
    """
    import ast

    tree = ast.parse(source)
    guarded: set[str] = set()
    names: set[str] = set()
    for node in tree.body:                       # body だけ = トップレベル
        # pytest.importorskip("X") を拾う。以降の import は守られている
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            is_skip = (isinstance(func, ast.Attribute) and func.attr == "importorskip") or (
                isinstance(func, ast.Name) and func.id == "importorskip"
            )
            if is_skip and call.args and isinstance(call.args[0], ast.Constant):
                guarded.add(str(call.args[0].value).split(".")[0])
            continue
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names - guarded


@pytest.mark.parametrize(
    "path",
    sorted((Path(__file__).parent).glob("test_*.py")),
    ids=lambda p: p.name,
)
def test_no_optional_dependency_is_imported_at_module_level(path):
    """任意依存はモジュール先頭で import しない。

    要るなら関数の中か、ファイル先頭の pytest.importorskip のあとで。
    """
    imported = _module_level_imports(path.read_text(encoding="utf-8"))
    offending = imported & set(OPTIONAL_AT_IMPORT_TIME)
    assert not offending, (
        f"{path.name} がモジュール先頭で {sorted(offending)} を import しています。"
        "最小構成では collection の時点で全体が止まります。\n"
        "  直す: 関数の中で import するか、ファイル先頭で\n"
        '         pytest.importorskip("<モジュール>")  を先に置いてください。'
    )
