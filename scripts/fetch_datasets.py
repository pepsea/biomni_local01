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
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from biomni_hypo.config import Settings, apply_biomni_env  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402

S3_BUCKET = "https://biomni-release.s3.amazonaws.com"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="取得するファイル名（省略時は許可リスト全件）")
    parser.add_argument("--list", action="store_true", help="取得せず一覧だけ表示する")
    args = parser.parse_args()

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

    data_lake = pathlib.Path(settings.data_path) / "biomni_data" / "data_lake"
    data_lake.mkdir(parents=True, exist_ok=True)
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

    check_and_download_s3_files(
        s3_bucket_url=S3_BUCKET,
        local_data_lake_path=str(data_lake),
        expected_files=missing,
        folder="data_lake",
    )
    print("完了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
