"""置き場所が使えるかを確かめ、駄目な理由をその場で調べる.

同じ調べ方を 3 か所で使う（ラン保存・データレイク・biomni のデータ置き場）。
「作れません」「開けません」だけでは打つ手が無い ── 権限なのか、
読み取り専用なのか、容量なのか、親が無いのかで対処が違う
（docs/design/27, 33）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class PathUnusable(RuntimeError):
    """その場所は使えない。理由を文字列にして持つ。"""


def _can_write(path: Path) -> bool:
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def existing_ancestor(path: Path) -> Path:
    """実在する一番近い親。どこから先が無いのかを言うため。

    exists() は、途中の親を辿れない（実行権が無い）と PermissionError を
    投げる。診断のためのコードが診断中に落ちては意味が無いので握ること
    （実測で踏んだ）。辿れない親は「無い」として上へ進む。
    """
    for candidate in [path, *path.parents]:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return Path(path.anchor or "/")


def is_read_only(path: Path, mounts_file: Path | None = None) -> bool:
    """そのパスを含むマウントが ro か（Linux のみ分かる）。"""
    try:
        mounts = (mounts_file or Path("/proc/mounts")).read_text(encoding="utf-8")
    except OSError:
        return False
    best, best_len = "", -1
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        point, opts = parts[1], parts[3]
        if str(path) == point or str(path).startswith(point.rstrip("/") + "/"):
            if len(point) > best_len:
                best, best_len = opts, len(point)
    return best.split(",")[0] == "ro" if best else False


def writable_candidate(name: str = "biomni-data") -> str:
    """実際に書けると確かめた逃げ場所を 1 つ返す。無ければ空文字。"""
    for base in (os.environ.get("HOME", ""), tempfile.gettempdir()):
        if not base:
            continue
        candidate = Path(base) / name
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".probe"
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError:
            continue
        return str(candidate)
    return ""


def describe_unusable(
    path: Path, exc: Exception | None = None, *, env_var: str = "", needed_bytes: int = 0
) -> str:
    """使えない理由を、その場で調べて並べる。

    ここで例外を出さないこと。理由を出すための関数が落ちると、
    元の理由まで失われる。
    """
    try:
        return _describe(path, exc, env_var=env_var, needed_bytes=needed_bytes)
    except Exception as inner:  # noqa: BLE001 - 診断は落ちてはいけない
        base = f"  {type(exc).__name__}: {exc}" if exc is not None else ""
        return f"{base}\n  （理由を調べる途中でも失敗しました: {type(inner).__name__}: {inner}）"


def _describe(
    path: Path, exc: Exception | None, *, env_var: str, needed_bytes: int
) -> str:
    facts: list[str] = []
    if exc is not None:
        facts.append(f"  {type(exc).__name__}: {exc}")

    near = existing_ancestor(path)
    facts.append(f"  実在する一番近い親 : {near}")
    facts.append(f"  そこに書けるか     : {_can_write(near)}")
    try:
        usage = shutil.disk_usage(near)
        facts.append(f"  空き容量           : {usage.free / 1e9:.1f} GB")
        if needed_bytes and usage.free < needed_bytes:
            facts.append(f"  → 空きが足りません（{needed_bytes / 1e9:.1f} GB ほど要ります）")
    except OSError:
        pass
    if is_read_only(near):
        facts.append("  → 読み取り専用でマウントされています")
    if str(near).startswith(("/mnt/", "/media/", "/net/")) or "nfs" in str(near):
        facts.append("  → マウントされた場所です。権限や容量はマウント元の設定に従います")

    facts += _ownership_facts(near)

    candidate = writable_candidate()
    if candidate and env_var:
        facts += [
            "",
            f"  書ける場所を 1 つ見つけました: {candidate}",
            "  .env にこの 1 行を書いて、もう一度実行してください:",
            f"      {env_var}={candidate}",
        ]
    elif env_var:
        facts.append(f"  .env の {env_var} を書ける場所にしてください。")
    return "\n".join(facts)


def in_container() -> bool:
    """コンテナの中で動いているか。直し方が変わるので見分ける。"""
    return Path("/.dockerenv").exists()


def _ownership_facts(near: Path) -> list[str]:
    """書けない原因が所有者の食い違いなら、そのまま直し方を出す。

    Docker では、bind マウントしたディレクトリの所有者はホスト側のまま。
    コンテナのユーザー（APP_UID、既定 1000）と食い違うと書けない。
    ホストで chmod しても、UID が違えば直らない（実測で踏んだ）。
    """
    if _can_write(near):
        return []
    facts: list[str] = []
    try:
        st = near.stat()
        me = os.getuid()
        facts.append(f"  ディレクトリの所有者: uid={st.st_uid} gid={st.st_gid}")
        facts.append(f"  いま動いている権限  : uid={me} gid={os.getgid()}")
        if st.st_uid != me:
            facts.append("  → 所有者が違います。権限ではなく UID の食い違いです。")
    except OSError:
        return facts

    if in_container():
        facts += [
            "",
            "  コンテナの中で動いています。ホスト側で所有者を合わせるか、",
            "  コンテナのユーザーをホストに合わせてください:",
            "      echo \"APP_UID=$(id -u)\" >> .env",
            "      echo \"APP_GID=$(id -g)\" >> .env",
            "      make update            # ビルド引数なので作り直しが要ります",
        ]
    else:
        facts += [
            "",
            "  所有者を自分に変える:",
            f"      sudo chown -R \"$(id -u):$(id -g)\" {near}",
        ]
    return facts


def ensure_writable_dir(path: Path | str, *, what: str, env_var: str = "") -> Path:
    """作って、書けることまで確かめる。駄目なら理由付きで投げる。

    biomni は A1.__init__ で <data_path>/biomni_data/benchmark を
    最初に makedirs する。書けない場所だと、利用者には見覚えのない
    "benchmark" というパスだけがエラーに出る。手前で確かめて、
    こちらの言葉で言うこと（docs/design/36）。
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathUnusable(
            f"{what}を作れません: {target}\n" + describe_unusable(target, exc, env_var=env_var)
        ) from exc
    if not _can_write(target):
        raise PathUnusable(
            f"{what}に書けません: {target}\n" + describe_unusable(target, None, env_var=env_var)
        )
    return target
