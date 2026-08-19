"""ノートブックが壊れていないことを CI で守る.

実行はしない（Ollama と biomni が要る）。JSON として妥当か、
コードセルが Python として構文的に正しいか、までを見る。
"""

import ast
import json
from pathlib import Path

import pytest

NOTEBOOK_DIR = Path(__file__).resolve().parent.parent / "notebooks"
NOTEBOOKS = sorted(NOTEBOOK_DIR.glob("*.ipynb"))


def test_notebooks_exist():
    assert [p.name for p in NOTEBOOKS] == [
        "00_environment_check.ipynb",
        "01_ollama_stop_sequence.ipynb",
        "02_agent_tracing.ipynb",
        "03_evidence_extraction.ipynb",
        "04_end_to_end.ipynb",
    ]


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_valid_json_with_cells(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["nbformat"] == 4
    assert doc["cells"]
    assert all(c["cell_type"] in ("code", "markdown") for c in doc["cells"])


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_code_cells_parse_as_python(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    for i, cell in enumerate(doc["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise AssertionError(f"{path.name} セル {i} が構文エラー: {exc}\n{source}") from exc


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebooks_have_no_stored_outputs(path):
    """出力を含めてコミットしない（差分が読めなくなるため）。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    for cell in doc["cells"]:
        if cell["cell_type"] == "code":
            assert cell.get("outputs") == []
            assert cell.get("execution_count") is None


def test_notebooks_import_the_shared_package_not_reimplement_it():
    """ノートブックにロジックを書かない方針を守る（def/class を置かない）。

    引っかかったら、そのロジックは biomni_hypo/ に移すこと。
    """
    allowed = {"def ask(llm):", "def on_event(kind, payload):"}  # 表示・薄いラッパのみ
    for path in NOTEBOOKS:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for cell in doc["cells"]:
            if cell["cell_type"] != "code":
                continue
            for line in cell["source"]:
                stripped = line.strip()
                if stripped.startswith(("def ", "class ")) and stripped not in allowed:
                    raise AssertionError(f"{path.name}: ノートブックに定義があります -> {stripped}")


def _bound_names(tree: ast.AST) -> set[str]:
    """そのセルで新しく定義される名前（代入・import・def/class・with as・for）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            names.update(a.arg for a in node.args.args + node.args.kwonlyargs)
            if node.args.vararg:
                names.add(node.args.vararg.arg)
            if node.args.kwarg:
                names.add(node.args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Lambda,)):
            names.update(a.arg for a in node.args.args + node.args.kwonlyargs)
    return names


def _loaded_names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_cells_do_not_use_names_defined_only_later(path):
    """セルの並び順が依存関係と合っているか。

    上から順に実行して NameError にならないことを静的に確認する。
    「後のセルでしか定義されない名前を、前のセルが使っている」だけを見るので、
    外部由来の名前を誤検出しない。
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    code_cells = [
        (i, ast.parse("".join(c["source"])))
        for i, c in enumerate(doc["cells"])
        if c["cell_type"] == "code"
    ]

    bound_per_cell = [(i, _bound_names(tree)) for i, tree in code_cells]

    for pos, (idx, tree) in enumerate(code_cells):
        defined_before: set[str] = set()
        for _j, names in bound_per_cell[: pos + 1]:
            defined_before |= names
        defined_later: set[str] = set()
        for _j, names in bound_per_cell[pos + 1 :]:
            defined_later |= names

        used_too_early = (_loaded_names(tree) - defined_before) & defined_later
        assert not used_too_early, (
            f"{path.name} セル {idx}: {sorted(used_too_early)} が後のセルでしか定義されていません。"
            " セルの順序を依存関係に合わせてください。"
        )
