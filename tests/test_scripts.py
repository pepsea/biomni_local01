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
import shutil
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


# ------------------------------------------- 使っていない方式のエラーを出さない
# 実測: Docker を使っていないのに「Docker デーモンに接続できません」で止まった。
# 常駐のさせ方が 3 通りあり、更新のたびにどれで動かしているかを思い出す必要が
# あった。update.sh は動いているものを見て選ぶ。


def test_update_does_not_fail_when_nothing_is_running(tmp_path):
    """何も常駐していないなら、Docker のエラーではなく次の一手を出すこと。"""
    empty = tmp_path / "bin"      # docker も systemctl も launchctl も無い PATH
    empty.mkdir()
    for tool in ("git", "curl", "sed", "seq", "sleep", "grep", "printf"):
        src = shutil.which(tool)
        if src:
            (empty / tool).symlink_to(src)

    proc = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts/update.sh"), "--no-pull"],
        capture_output=True, text=True, timeout=120, cwd=ROOT,
        env={"PATH": f"{empty}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "常駐していません" in proc.stdout
    assert "scripts/start.sh" in proc.stdout, "次の一手が無い"
    assert "デーモンに接続できません" not in proc.stdout, "使っていない方式のエラーを出している"


def test_update_help_does_not_touch_anything(tmp_path):
    proc = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts/update.sh"), "--help"],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )
    assert proc.returncode == 0
    assert "--no-pull" in proc.stdout
    assert "常駐していません" not in proc.stdout, "--help なのに実行している"


# --------------------------------------- 他のツールと Ollama を取り合っている
# 同じ Ollama を別のツールと共有すると、モデルの入れ替えとメモリの取り合いで
# 生成が極端に遅くなり、こちらは打ち切られて <solution> に届かない。
# 「読み込まれているモデル」を見れば気付けるので、切り分けに入れる。


class _FakeOllama:
    """/api/tags と /api/ps を返すだけの Ollama。"""

    def __init__(self, loaded: list[str], spaced: bool):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        tags = {"models": [{"name": "qwen3:14b", "model": "qwen3:14b"}]}
        ps = {"models": [{"model": m} for m in loaded]}
        # コロンの後ろの空白の有無は実装で変わる。両方を試す
        dump = (lambda o: json.dumps(o)) if spaced else (lambda o: json.dumps(o, separators=(",", ":")))

        class H(BaseHTTPRequestHandler):
            def _send(self, obj):
                body = dump(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._send(ps if self.path.endswith("/api/ps") else tags)

            def log_message(self, *a):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def _diagnose(port: int) -> str:
    import os

    proc = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts/diagnose-ollama.sh")],
        capture_output=True, text=True, timeout=120, cwd=ROOT,
        env={**os.environ, "OLLAMA_PORT": str(port),
             "OLLAMA_BASE_URL": f"http://localhost:{port}", "HYPO_MODEL": "qwen3:14b"},
    )
    return proc.stdout


@pytest.mark.parametrize("spaced", [True, False], ids=["空白あり", "空白なし"])
def test_other_models_loaded_are_reported(spaced):
    """他のツールのモデルが載っていたら名指しすること。"""
    with _FakeOllama(["qwen3:14b", "gemma3:27b"], spaced) as mock:
        out = _diagnose(mock.port)

    assert "gemma3:27b" in out, out
    assert "以外も読み込まれています" in out


@pytest.mark.parametrize("spaced", [True, False], ids=["空白あり", "空白なし"])
def test_our_model_alone_is_not_a_warning(spaced):
    with _FakeOllama(["qwen3:14b"], spaced) as mock:
        out = _diagnose(mock.port)

    assert "だけが読み込まれています" in out, out
    assert "以外も読み込まれています" not in out


# ------------------------------------------------- データセット取得の失敗理由
# 実測: 取得が失敗すると、原因を問わず「ネットワークを確認」と出していた。
# 実際には置き場所（相対パス）の問題だった。


def _fetch_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetch_datasets", ROOT / "scripts" / "fetch_datasets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PermissionError("denied"), "書き込み権限"),
        (FileNotFoundError("no such file or directory"), "相対パス"),
        (OSError(28, "No space left on device"), "ディスクの空き"),
        (TimeoutError("timed out"), "ネットワーク"),
        (ValueError("何か別のこと"), "そのまま報告"),
    ],
)
def test_download_failures_are_told_apart(exc, expected):
    module = _fetch_module()
    hint = module._download_hint(exc, Path("/tmp/dl"))
    assert expected in hint, hint


def test_an_unwritable_data_lake_is_named(tmp_path, capsys):
    """作れない置き場所は、ネットワークのせいにしないこと。"""
    import os

    module = _fetch_module()
    os.environ["HYPO_SKIP_DOTENV"] = "1"
    os.environ["BIOMNI_PATH"] = "/proc/nowhere"
    try:
        rc = module.main(["--only", "gwas_catalog.pkl"])
    finally:
        os.environ.pop("BIOMNI_PATH", None)

    err = capsys.readouterr().err
    assert rc == 1
    assert "データレイクを作れません" in err
    assert "BIOMNI_PATH" in err
    assert "ネットワーク" not in err, "見当違いの案内をしている"


# ------------------------------------------- 「作れません」で終わらせない
# 実測: 「データレイクを作れません」だけでは打つ手が無い。権限・読み取り専用・
# 容量・親が無い、で対処が違う。その場で調べて、書ける場所まで示す。


def test_a_read_only_mount_is_named(tmp_path):
    module = _fetch_module()
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "server:/vol /mnt/storage nfs4 ro,relatime 0 0\n",
        encoding="utf-8",
    )
    assert module._is_read_only(Path("/mnt/storage/users/x"), mounts) is True
    assert module._is_read_only(Path("/home/x"), mounts) is False


def test_the_nearest_existing_parent_is_reported(tmp_path):
    module = _fetch_module()
    deep = tmp_path / "a" / "b" / "c"
    assert module._existing_ancestor(deep) == tmp_path


def test_a_writable_place_is_actually_verified(tmp_path, monkeypatch):
    """提案する場所は、実際に書けると確かめたものにすること。"""
    module = _fetch_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    found = module._writable_candidate()

    assert found == str(tmp_path / "biomni-data")
    assert Path(found).is_dir()
    assert not list(Path(found).glob(".probe")), "試した跡を残さないこと"


def test_the_failure_names_a_place_to_use(tmp_path, monkeypatch, capsys):
    """作れないときは、代わりに使える場所まで出すこと。"""
    import os

    module = _fetch_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPO_SKIP_DOTENV", "1")
    monkeypatch.setenv("BIOMNI_PATH", "/proc/nowhere")
    rc = module.main(["--only", "gwas_catalog.pkl"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "実在する一番近い親" in err
    assert f"BIOMNI_PATH={tmp_path / 'biomni-data'}" in err, err
    assert os.access(tmp_path / "biomni-data", os.W_OK)


# ------------------------------------- bind マウントの所有者とコンテナの UID
# 実測: /app/data/biomni_data/data_lake に permission がない、で止まった。
# ./data は bind マウントなので所有者はホスト側のまま。コンテナのユーザー
# （APP_UID、既定 1000）と食い違うと書けない。ホストで chmod しても直らない。


def _preflight_in(repo: Path, env: dict) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(  # noqa: S603
        ["bash", str(repo / "scripts/docker-preflight.sh")],
        capture_output=True, text=True, timeout=120, cwd=repo,
        env={**os.environ, **env},
    )


@pytest.fixture
def repo_copy(tmp_path):
    """.env を書き換えるので、リポジトリを汚さないよう複製して試す。"""
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    for name in ("docker-preflight.sh",):
        shutil.copy(ROOT / "scripts" / name, work / "scripts" / name)
    shutil.copy(ROOT / ".env.example", work / ".env.example")
    (work / ".env").write_text("APP_PORT=5002\nOLLAMA_BASE_URL=http://localhost:11434\n",
                               encoding="utf-8")
    return work


def test_app_uid_is_aligned_with_the_host(repo_copy, tmp_path):
    """APP_UID がホストと違えば .env を合わせること。"""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/usr/bin/env bash\ncase \"$1\" in info) exit 0;; *) exit 0;; esac\n")
    (fake / "docker").chmod(0o755)

    proc = _preflight_in(repo_copy, {"PATH": f"{fake}:/usr/bin:/bin"})
    env_text = (repo_copy / ".env").read_text(encoding="utf-8")

    import os
    if os.getuid() == 0:
        # root では書き込まない（コンテナの権限を落としている意味が無くなる）
        assert "root で実行しています" in proc.stdout
        assert "APP_UID=" not in env_text
    else:
        assert f"APP_UID={os.getuid()}" in env_text
        assert f"APP_GID={os.getgid()}" in env_text


def test_running_twice_is_quiet(repo_copy, tmp_path):
    """2 回目は「一致」と言うだけで、書き換えを繰り返さないこと。"""
    import os

    if os.getuid() == 0:
        pytest.skip("root では APP_UID を書かない")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake / "docker").chmod(0o755)

    _preflight_in(repo_copy, {"PATH": f"{fake}:/usr/bin:/bin"})
    second = _preflight_in(repo_copy, {"PATH": f"{fake}:/usr/bin:/bin"})
    assert "ホストと一致" in second.stdout
    assert (repo_copy / ".env").read_text(encoding="utf-8").count("APP_UID=") == 1


# ------------------------------------------------- 文献検索が本当に動くか
# 「使えるはず」ではなく「使える」を見るためのもの。ネットワークが無い環境でも
# 失敗の理由が読めること（実測: すべて外向き通信が塞がれた環境で確認した）。


def _literature_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_literature", ROOT / "scripts" / "check-literature.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_denied_tool_is_not_reported_as_a_failure():
    """ポリシーで外しているものは、失敗ではない（意図した状態）。"""
    from biomni_hypo.policy import ResourcePolicy

    module = _literature_module()
    assert module._check("query_scholar", "x", ResourcePolicy.load()) is True


def test_a_tool_error_is_reported_as_a_failure(capsys):
    """biomni は "Error querying PubMed: ..." と返す。"Error:" だけ見ると取り逃す。"""
    from biomni_hypo.policy import ResourcePolicy

    module = _literature_module()
    module._load = lambda name: (lambda q: "Error querying PubMed: no network", "")
    assert module._check("query_pubmed", "x", ResourcePolicy.load()) is False
    assert "Error querying PubMed" in capsys.readouterr().out


def test_results_with_identifiers_pass(capsys):
    from biomni_hypo.policy import ResourcePolicy

    module = _literature_module()
    module._load = lambda name: (
        lambda q: "Title: X\nIDs: PMID:37821999 PMC10592456\nAbstract: …", ""
    )
    assert module._check("query_europepmc", "x", ResourcePolicy.load()) is True
    out = capsys.readouterr().out
    assert "リンク可" in out
    assert "https://pubmed.ncbi.nlm.nih.gov/37821999/" in out
