#!/usr/bin/env python
"""ローカル（Ollama）のモデルを一覧して、使えるものを表示する.

    python scripts/list_models.py                  # 一覧
    python scripts/list_models.py --json           # JSON で出す
    python scripts/list_models.py --set qwen3:8b   # .env の HYPO_MODEL を書き換える

判定基準は Web アプリ・ノートブックと同じ（biomni_hypo.models）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from biomni_hypo.config import Settings  # noqa: E402
from biomni_hypo.models import list_local_models  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"


def set_env_model(name: str, provider: str = "ollama") -> None:
    """.env の HYPO_MODEL と BIOMNI_LLM を書き換える。

    BIOMNI_LLM も一緒に変える必要がある。biomni の DB クエリツールは
    A1 のコンストラクタ引数ではなく default_config を見るため（docs/design/04 §4.3）。
    """
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    updates = {"HYPO_MODEL": name, "BIOMNI_LLM": name, "HYPO_PROVIDER": provider}
    for key, value in updates.items():
        pattern = re.compile(rf"^{key}=")
        replaced = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ .env を更新しました: HYPO_PROVIDER={provider} / HYPO_MODEL={name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument("--set", metavar="MODEL", help="このモデルを .env の既定にする")
    args = parser.parse_args()

    settings = Settings()
    policy = ResourcePolicy.load(settings.policy_path)
    catalog = list_local_models(settings, policy)

    if args.json:
        print(
            json.dumps(
                {
                    "reachable": catalog.reachable,
                    "base_url": catalog.base_url,
                    "error": catalog.error,
                    "models": [m.as_dict() for m in catalog.models],
                    "selectable": [m.name for m in catalog.selectable],
                    "default": (catalog.default(settings.model) or None)
                    and catalog.default(settings.model).name,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if catalog.reachable else 1

    print(f"Ollama: {catalog.base_url}")
    if not settings.anthropic_api_key:
        print("Claude API: ANTHROPIC_API_KEY 未設定（設定するとクラウドのモデルも選べます）")
    else:
        print("Claude API: 利用可能")
    print()
    print(catalog.as_table())
    print()
    default = catalog.default(settings.model)
    print(f"設定中のモデル: {settings.provider} / {settings.model}")
    print(f"既定で選ばれる : {default.name if default else '（なし）'}")

    if args.set:
        target = catalog.get(args.set)
        if target is None:
            print(f"\n✗ {args.set} はローカルにありません。まず `ollama pull {args.set}`", file=sys.stderr)
            return 1
        if not target.allowed:
            print(f"\n✗ {args.set} は商用利用ポリシーで不可: {target.reason}", file=sys.stderr)
            return 1
        print()
        set_env_model(args.set, target.provider)
        if not target.local:
            print("⚠️  クラウドのモデルです。質問文と実行結果が外部に送信されます。")

    return 0 if catalog.reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
