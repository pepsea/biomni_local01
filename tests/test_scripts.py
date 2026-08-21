"""シェルスクリプトの静的検査.

実際に踏んだバグ:
  scripts/docker-preflight.sh:85 で `$OLLAMA_URL）` と書いていた。
  変数の直後が全角括弧（マルチバイト）で、古い bash（macOS 標準は 3.2）が
  識別子の切れ目を取り違え、`set -u` の下で "unbound variable" になった。

    scripts/docker-preflight.sh: line 85: OLLAMA_URL?: unbound variable
    make: *** [docker-rebuild] Error 1

  日本語のメッセージを出すスクリプトでは踏みやすい。機械的に防ぐ。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.sh"))

#: $VAR の直後が非 ASCII。${VAR} と書けば安全
BARE_BEFORE_MULTIBYTE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


@pytest.mark.parametrize("script", SCRIPTS, ids=_ids(SCRIPTS))
def test_syntax_is_valid(script: Path) -> None:
    proc = subprocess.run(  # noqa: S603
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=_ids(SCRIPTS))
def test_no_bare_variable_before_multibyte(script: Path) -> None:
    """`$VAR（…）` を禁じる。`${VAR}（…）` と書くこと。

    bash 5 は正しく扱うが、macOS 標準の bash 3.2 は取り違える。
    利用者の環境を選ばないようにする。
    """
    text = script.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in BARE_BEFORE_MULTIBYTE.finditer(line):
            hits.append(f"{script.name}:{lineno}  ${m.group(1)} → ${{{m.group(1)}}}")
    assert not hits, "変数の直後が全角文字です:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("script", SCRIPTS, ids=_ids(SCRIPTS))
def test_set_u_scripts_are_shellcheck_clean_enough(script: Path) -> None:
    """`set -u` を使うなら、参照する変数は必ず先に代入されていること。

    shellcheck があれば使う。無ければスキップ（CI 環境を選ばない）。
    """
    if not _has_shellcheck():
        pytest.skip("shellcheck が入っていません")
    proc = subprocess.run(  # noqa: S603
        ["shellcheck", "--severity=error", "--format=gcc", str(script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout or proc.stderr


def _has_shellcheck() -> bool:
    try:
        subprocess.run(  # noqa: S603
            ["shellcheck", "--version"], capture_output=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def test_every_script_is_executable() -> None:
    not_exec = [s.name for s in SCRIPTS if not s.stat().st_mode & 0o111]
    assert not not_exec, f"実行権限がありません: {not_exec}"


@pytest.mark.skipif(sys.platform == "win32", reason="bash 前提")
def test_help_does_not_crash() -> None:
    """--help が落ちないこと（引数解析の壊れを拾う）。"""
    for script in SCRIPTS:
        if "--help" not in script.read_text(encoding="utf-8"):
            continue
        proc = subprocess.run(  # noqa: S603
            ["bash", str(script), "--help"], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"{script.name} --help: {proc.stderr}"
