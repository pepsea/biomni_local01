#!/usr/bin/env python
"""許可リストにあるデータセットだけを取得する.

A1.__init__ に任せると env_desc に列挙された全ファイル（数十 GB）を取りに行くため、
データ取得はこのスクリプトに一本化する（docs/design/04 §4.4, docs/design/05 §5.2 強制ポイント 1）。

使い方:
    python scripts/fetch_datasets.py                 # 許可リスト全件
    python scripts/fetch_datasets.py --only gwas_catalog.pkl gene_info.parquet
    python scripts/fetch_datasets.py --list          # 取得せず一覧だけ表示
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from biomni_hypo.config import Settings, apply_biomni_env  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402

S3_BUCKET = "https://biomni-release.s3.amazonaws.com"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="取得するファイル名（省略時は許可リスト全件）")
    parser.add_argument("--list", action="store_true", help="取得せず一覧だけ表示する")
    args = parser.parse_args(argv)

    settings = Settings()
    apply_biomni_env(settings)
    policy = ResourcePolicy.load(settings.policy_path)

    targets = args.only or policy.allowed_dataset_names()

    rejected = [name for name in targets if not policy.check_dataset(name).allowed]
    if rejected:
        print("以下は商用利用の許可リストにありません。取得しません:", file=sys.stderr)
        for name in rejected:
            print(f"  ✕ {name}: {policy.check_dataset(name).reason}", file=sys.stderr)
        return 1

    # 設定は絶対パスに解決済みだが、ここでも確かめる。相対のまま渡ると
    # 実行した場所によって別の場所を指す（実測で踏んだ）
    data_lake = (pathlib.Path(settings.data_path) / "biomni_data" / "data_lake").resolve()
    try:
        data_lake.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"データレイクを作れません: {data_lake}", file=sys.stderr)
        print(_why_cannot_use(data_lake, exc), file=sys.stderr)
        return 1
    if not os.access(data_lake, os.W_OK):
        print(f"データレイクに書けません: {data_lake}", file=sys.stderr)
        print(_why_cannot_use(data_lake, None), file=sys.stderr)
        return 1
    present = {p.name for p in data_lake.glob("*")}

    print(f"データレイク: {data_lake}")
    for name in targets:
        d = policy.check_dataset(name)
        mark = "✓" if name in present else "·"
        flag = "  ⚠️ 要ライセンス確認" if d.review_required else ""
        print(f"  {mark} {name:52s} {d.license:14s} {d.attribution}{flag}")

    if args.list:
        return 0

    missing = [n for n in targets if n not in present]
    if not missing:
        print("\nすべて取得済みです。")
        return 0

    print(f"\n{len(missing)} 件を取得します...")
    from biomni.utils import check_and_download_s3_files

    try:
        check_and_download_s3_files(
            s3_bucket_url=S3_BUCKET,
            local_data_lake_path=str(data_lake),
            expected_files=missing,
            folder="data_lake",
        )
    except Exception as exc:  # noqa: BLE001 - 原因を分けて出すのがここの仕事
        # 何でも「ネットワークを確認」と言っていたので、権限もディスクも
        # 見当違いの案内になっていた（実測で踏んだ）
        print(f"\n取得に失敗しました: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(_download_hint(exc, data_lake), file=sys.stderr)
        return 1

    got = {p.name for p in data_lake.glob("*")}
    still = [n for n in missing if n not in got]
    if still:
        print(f"\n取得できなかったものがあります: {', '.join(still)}", file=sys.stderr)
        print(f"  置き場所: {data_lake}", file=sys.stderr)
        return 1
    print("完了。")
    return 0


#: データレイクに要るおおよその容量（許可リスト最小構成で 200MB ほど）
_NEEDED_BYTES = 300 * 1024 * 1024


def _existing_ancestor(path: pathlib.Path) -> pathlib.Path:
    """実在する一番近い親を返す。どこから先が無いのかを言うため。"""
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    return pathlib.Path("/")


def _writable_candidate() -> str:
    """実際に書けると確かめた置き場所を 1 つ返す。空文字なら見つからない。"""
    for base in (os.environ.get("HOME", ""), tempfile.gettempdir()):
        if not base:
            continue
        candidate = pathlib.Path(base) / "biomni-data"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".probe"
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError:
            continue
        return str(candidate)
    return ""


def _why_cannot_use(path: pathlib.Path, exc: Exception | None) -> str:
    """その場で調べて理由を添える。

    「作れません」だけでは打つ手が無い。権限なのか、読み取り専用なのか、
    容量なのか、親が無いのかで対処が違う（docs/design/27 と同じ考え方）。
    """
    facts: list[str] = []
    if exc is not None:
        facts.append(f"  {type(exc).__name__}: {exc}")

    near = _existing_ancestor(path)
    facts.append(f"  実在する一番近い親 : {near}")
    facts.append(f"  そこに書けるか     : {os.access(near, os.W_OK)}")

    try:
        usage = shutil.disk_usage(near)
        facts.append(f"  空き容量           : {usage.free / 1e9:.1f} GB")
        if usage.free < _NEEDED_BYTES:
            facts.append("  → 空きが足りません（最小構成で 0.3 GB ほど要ります）")
    except OSError:
        pass

    if _is_read_only(near):
        facts.append("  → 読み取り専用でマウントされています")
    resolved = str(near)
    if resolved.startswith(("/mnt/", "/media/", "/net/")) or "nfs" in resolved:
        facts.append("  → マウントされた場所です。権限や容量はマウント元の設定に従います")

    candidate = _writable_candidate()
    if candidate:
        facts.append("")
        facts.append(f"  書ける場所を 1 つ見つけました: {candidate}")
        facts.append("  .env にこの 1 行を書いて、もう一度実行してください:")
        facts.append(f"      BIOMNI_PATH={candidate}")
    else:
        facts.append("  .env の BIOMNI_PATH を書ける場所にしてください。")
    return "\n".join(facts)


def _is_read_only(path: pathlib.Path, mounts_file: pathlib.Path | None = None) -> bool:
    """そのパスを含むマウントが ro かどうか（Linux のみ分かる）。

    mounts_file はテスト用。既定は /proc/mounts。
    """
    try:
        mounts = (mounts_file or pathlib.Path("/proc/mounts")).read_text(encoding="utf-8")
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


def _download_hint(exc: Exception, data_lake: pathlib.Path) -> str:
    """例外から、次に見るべきところを 1 つに絞る。"""
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, PermissionError) or "permission" in text:
        return f"  書き込み権限がありません: {data_lake}"
    if isinstance(exc, FileNotFoundError) or "no such file" in text:
        return (
            f"  置き場所が見つかりません: {data_lake}\n"
            "  .env の BIOMNI_PATH が相対パスだと、実行した場所によって指す先が変わります。\n"
            "  絶対パスで指定してください（例: BIOMNI_PATH=$HOME/biomni-data）。"
        )
    if "no space" in text or isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "  ディスクの空きがありません。df -h で確認してください。"
    if any(k in name.lower() or k in text for k in ("timeout", "connection", "resolve", "ssl", "network", "dns")):
        return "  ネットワークに繋がりません。プロキシや DNS を確認してください。"
    return "  上のメッセージをそのまま報告してください。"


if __name__ == "__main__":
    raise SystemExit(main())
