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
