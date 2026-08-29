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
from typing import Any

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


# ------------------------------------------------- docker-preflight の早期失敗
# 実測: Docker Desktop が止まっているのに、ポートの確認まで済ませてから
#   unable to get image ...: Cannot connect to the Docker daemon
# で落ちていた。先に確認して、Docker 無しで動かす道も示す。


def _run_preflight(env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    root = Path(__file__).resolve().parents[1]
    return subprocess.run(  # noqa: S603
        ["bash", str(root / "scripts/docker-preflight.sh")],
        capture_output=True, text=True, timeout=120,
        cwd=root, env={**os.environ, **(env or {})},
    )


def test_preflight_fails_fast_without_a_docker_daemon(tmp_path):
    """docker が使えないなら、その場で止めて理由を出すこと。"""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/usr/bin/env bash\nexit 1\n")
    (fake / "docker").chmod(0o755)

    proc = _run_preflight({"PATH": f"{fake}:/usr/bin:/bin"})
    assert proc.returncode == 1
    assert "Docker デーモンに接続できません" in proc.stdout
    assert "scripts/start.sh" in proc.stdout, "Docker 無しで動かす道を示すこと"
    # ポートの確認まで進まないこと（進んでも意味が無い）
    assert "APP_PORT" not in proc.stdout


def test_preflight_flags_a_stale_compose_profile(tmp_path):
    """COMPOSE_PROFILES=ollama は .env に残りやすい（.env は git 管理外）。"""
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/docker-preflight.sh").read_text(encoding="utf-8")
    assert "この profile はもうありません" in text
    assert "set-provider.sh ollama" in text


# ------------------------------------------------------- ポートを誰が掴むか
# 実測: 素の uvicorn（scripts/start.sh）が 8001 を掴んだまま make docker-rebuild
# を叩くと、preflight は通るのに docker が落ちた。
#
#   failed to bind host port 0.0.0.0:8001/tcp: address already in use
#
# 「自分のサービスが動いているか」で代用していたため。実際の掴み主を見る。


def _fake_docker(tmp_path, ports: str = "") -> Path:
    """docker が動いていて、biomni-app が `ports` を公開している状態を作る。"""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  info*) exit 0 ;;\n"
        f"  ps*--filter*) printf '%s\\n' '{ports}' ;;\n"
        "  compose*ps*) echo app ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (fake / "docker").chmod(0o755)
    return fake


def _busy_port() -> tuple[int, Any]:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock.getsockname()[1], sock


def test_preflight_fails_when_something_else_holds_the_port(tmp_path):
    """コンテナが動いていても、掴み主が別プロセスなら止めること。"""
    port, sock = _busy_port()
    try:
        fake = _fake_docker(tmp_path, ports="")     # biomni-app はポートを公開していない
        proc = _run_preflight({
            "PATH": f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin",
            "APP_PORT": str(port),
        })
    finally:
        sock.close()

    assert f"APP_PORT={port} は既に使われています" in proc.stdout
    assert "bash scripts/start.sh" in proc.stdout, "Docker 無しの道を示すこと"
    assert proc.returncode == 1


def test_preflight_allows_a_port_held_by_our_own_container(tmp_path):
    """自分のコンテナが掴んでいるだけなら、再ビルドできるので止めない。"""
    port, sock = _busy_port()
    try:
        fake = _fake_docker(tmp_path, ports=f"0.0.0.0:{port}->8000/tcp")
        proc = _run_preflight({
            "PATH": f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin",
            "APP_PORT": str(port),
        })
    finally:
        sock.close()

    assert f"APP_PORT={port} は使えます" in proc.stdout


# ------------------------------------------------------- 既定ポートの食い違い
# ホスト側の待ち受けポートは .env.example / compose / Makefile / 各スクリプトの
# 5 箇所以上に既定値として書いてある。1 箇所だけ変えると、起動したポートと
# 案内された URL が食い違い、「開かない」の原因が分からなくなる。
#
# コンテナ内のポート（Dockerfile の EXPOSE、compose の右辺、healthcheck）は
# 別物なので対象にしない。

ROOT = Path(__file__).resolve().parents[1]

#: (ファイル, 既定値を取り出す正規表現)
DEFAULT_PORT_SOURCES = (
    (".env.example", r"^APP_PORT=(\d+)"),
    ("Makefile", r"APP_PORT := \$\(if \$\(APP_PORT\),\$\(APP_PORT\),(\d+)\)"),
    ("docker-compose.yml", r"\$\{APP_PORT:-(\d+)\}:\d+"),
    ("scripts/start.sh", r'PORT="\$\{APP_PORT:-(\d+)\}"'),
    ("scripts/docker-preflight.sh", r'APP_PORT="\$\{APP_PORT:-(\d+)\}"'),
    ("scripts/install-local-service.sh", r'PORT="\$\{PORT:-(\d+)\}"'),
    ("scripts/install-service.sh", r'PORT="\$\{PORT:-(\d+)\}"'),
    ("scripts/diagnose-app.sh", r'PORT="\$\{PORT:-(\d+)\}"'),
    ("scripts/diagnose-models.sh", r'PORT="\$\{PORT:-(\d+)\}"'),
    ("scripts/set-provider.sh", r'PORT_NOW="\$\{PORT_NOW:-(\d+)\}"'),
)


def test_the_default_port_is_the_same_everywhere() -> None:
    found: dict[str, str] = {}
    for name, pattern in DEFAULT_PORT_SOURCES:
        text = (ROOT / name).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE)
        assert match, f"{name} から既定ポートを読めません（書き方を変えたなら正規表現も直すこと）"
        found[name] = match.group(1)

    assert len(set(found.values())) == 1, "既定ポートが食い違っています:\n  " + "\n  ".join(
        f"{k}: {v}" for k, v in found.items()
    )


# ------------------------------------------------- Docker のラン履歴の置き場
# 実測: 画面に「/app/workspace/runs.sqlite3 を開けません」と出た。
# /app/workspace は ./workspace の bind マウント。リポジトリがネットワーク
# マウントの上にあると sqlite を開けないので、ホスト側の置き場所の制約を
# コンテナがそのまま引き継いでしまう。


def _compose() -> dict:
    yaml = pytest.importorskip("yaml", reason="PyYAML が無い環境ではスキップ")
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_the_run_store_is_not_on_a_bind_mount() -> None:
    """sqlite の置き場は bind マウントの下にしないこと。"""
    app = _compose()["services"]["app"]
    workspace = app["environment"]["HYPO_WORKSPACE"]
    default = workspace.split(":-", 1)[1].rstrip("}") if ":-" in workspace else workspace

    binds = [m.split(":")[1] for m in app["volumes"] if m.startswith(".")]
    for bind in binds:
        assert not default.startswith(bind), (
            f"HYPO_WORKSPACE の既定 {default} が bind マウント {bind} の下にあります。"
            "リポジトリの置き場所の制約をコンテナが引き継ぎます"
        )


def test_the_run_store_survives_a_rebuild() -> None:
    """名前付きボリュームに載せること（再ビルドで履歴が消えないように）。"""
    compose = _compose()
    app = compose["services"]["app"]
    workspace = app["environment"]["HYPO_WORKSPACE"]
    default = workspace.split(":-", 1)[1].rstrip("}") if ":-" in workspace else workspace

    named = {m.split(":")[0]: m.split(":")[1] for m in app["volumes"] if not m.startswith(".")}
    mounted_at = [path for name, path in named.items() if name in (compose.get("volumes") or {})]
    assert any(default.startswith(path) for path in mounted_at), (
        f"HYPO_WORKSPACE の既定 {default} が名前付きボリューム {mounted_at} の下にありません"
    )
