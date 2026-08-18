# biomni_local01 — Biomni 仮説構築 Web アプリ

生物医学の研究課題を入力すると、[Biomni](https://github.com/snap-stanford/biomni) エージェントが
文献・公共データベース・データレイクを調べ、**検証可能な仮説**と**その根拠（使用データ・出典・実行過程）**を
セットで返すアプリケーション。

- LLM は **Ollama（ローカル実行）**。API キー不要、質問文と自前データがローカルから出ない。
- **商用利用可能なリソースのみ**を使う（データセット・モデル重み・ライブラリ・外部 API）。
- Jupyter ノートブックで検証しながら、同じコードで Web アプリを動かす。

## 現在の状態

| レイヤ | 状態 |
| --- | --- |
| 設計 (`docs/design/`) | ✅ 01〜09 |
| コアパッケージ (`biomni_hypo/`) | ✅ 実装済み・テスト 96 件 |
| 検証ノートブック (`notebooks/`) | ✅ 5 本 |
| API + SSE (`backend/`) | ✅ 実装済み |
| React フロントエンド | ⬜ 未着手（設計は [07](docs/design/07-ui-design.md)） |

**実機（Ollama + biomni）での動作確認は未実施。** テストは外部サービスを使わない範囲のみを
検証している。実機での確認手順は [09 §9.6](docs/design/09-implementation.md#96-実機で確認すべきこと)。

## クイックスタート

### 1. まずテストだけ動かす（依存 3 つ）

```bash
python -m venv .venv && source .venv/bin/activate
pip install pydantic pyyaml requests pytest
pytest -q                       # 96 passed
```

`biomni` も Ollama も要らない。根拠の抽出・検証・レポート生成のロジックはここで確認できる。

### 2. 全部入れる

```bash
pip install -r requirements.txt
cp .env.example .env
ollama pull qwen3:14b
python scripts/fetch_datasets.py --only gwas_catalog.pkl gene_info.parquet
```

### 3. ノートブックで検証する

```bash
jupyter lab notebooks/
```

| ノートブック | 内容 | Ollama |
| --- | --- | --- |
| [00_environment_check](notebooks/00_environment_check.ipynb) | 環境の 5 項目チェック。ここが緑になるまで先に進まない | 要 |
| [01_ollama_stop_sequence](notebooks/01_ollama_stop_sequence.ipynb) | **最重要**。stop シーケンスが効くかの A/B 検証 | 要 |
| [02_agent_tracing](notebooks/02_agent_tracing.ipynb) | A1 の構築と実行トレースの構造化 | 要 |
| [03_evidence_extraction](notebooks/03_evidence_extraction.ipynb) | 根拠の抽出・検証・レポート | **不要** |
| [04_end_to_end](notebooks/04_end_to_end.ipynb) | 本番と同じ `run_hypothesis()` で受け入れ基準を測る | 要 |

### 4. Web アプリを起動する

```bash
make api            # uvicorn backend.app.main:app --port 8000
```

```bash
curl -X POST localhost:8000/api/runs -H 'content-type: application/json' \
  -d '{"question": "TNBC で PARP 阻害剤耐性を規定する因子は？"}'
curl -N localhost:8000/api/runs/<run_id>/events     # SSE でトレースが流れる
curl -s localhost:8000/api/runs/<run_id>/report     # Markdown レポート
```

Docker なら `docker compose up`（ollama + api）。

## 構成

```
biomni_hypo/     共有コアパッケージ ← ノートブックも Web アプリもここを呼ぶ
notebooks/       検証ハーネス（ロジックは書かない。テストで強制）
backend/app/     FastAPI + SSE + ラン実行ワーカー（子プロセス）
config/          resource_policy.yaml（商用限定・既定拒否）
scripts/         データセット取得
tests/           96 件。外部サービス不要で 1 秒未満
docs/design/     設計書
```

## 設計書

| ドキュメント | 内容 |
| --- | --- |
| [01-overview](docs/design/01-overview.md) | 目的・スコープ・ユースケース・非機能要件 |
| [02-architecture](docs/design/02-architecture.md) | システム構成・コンポーネント・実行シーケンス |
| [03-evidence-model](docs/design/03-evidence-model.md) | **根拠モデル**（データモデル・抽出・検証） |
| [04-ollama-integration](docs/design/04-ollama-integration.md) | Ollama 統合と Biomni 側の落とし穴 |
| [05-commercial-licensing](docs/design/05-commercial-licensing.md) | **商用利用限定**の設計（リソースポリシー） |
| [06-api-spec](docs/design/06-api-spec.md) | REST / SSE API 仕様 |
| [07-ui-design](docs/design/07-ui-design.md) | 画面設計 |
| [08-roadmap](docs/design/08-roadmap.md) | フェーズ計画・受け入れ基準・リスク |
| [09-implementation](docs/design/09-implementation.md) | 実装の構成（ノートブックと Web アプリの関係） |

## 設計の要点

1. **`A1.go_stream()` を使わない。** 整形済み文字列しか返さず根拠抽出に必要な情報が失われるため、
   A1 内部の LangGraph を直接ストリームして構造化する（`biomni_hypo/tracing.py`）。
2. **仮説の生成と JSON 化を分離する。** トレースから機械抽出した根拠候補を渡し、
   **その ID からしか選べない**制約下で仮説を書かせる（`biomni_hypo/extractor.py`）。
3. **引用は実在検証を通してから表示する。** 「コードで触れていないのに引用された」ものは
   幻覚として隔離する（`biomni_hypo/verifier.py`）。
4. **商用利用は `commercial_mode=True` だけでは足りない。** Biomni の同フラグはデータセットを
   絞るがツールは絞らないため、独自ポリシーで既定拒否を敷く（`biomni_hypo/policy.py`）。

## 既知の落とし穴（biomni 0.0.8 / GitHub main）

`biomni_hypo/agent_factory.py` にすべて封じ込めてある。詳細は [04](docs/design/04-ollama-integration.md)。

| # | 問題 | 対策 |
| --- | --- | --- |
| §4.1 | `get_llm()` の **Ollama 分岐だけ `stop_sequences` を渡していない** → モデルが実行結果を捏造する | `agent.llm` を `ChatOllama(stop=...)` に差し替え |
| §4.2 | 同分岐は `base_url` も無視する | 同上 |
| §4.3 | `database.py` は `default_config.llm`（既定 Claude）を使う → DB ツールが外部 API を叩く | `apply_biomni_env()` を import 前に実行 |
| §4.4 | `A1.__init__` がデータレイクを一括ダウンロードする | `expected_data_lake_files` を明示 |
| §4.5 | リソース検索プロンプトが巨大で `num_ctx=2048` では機能しない | モジュール絞り込み + v1 は既定 OFF |

## ライセンス

Apache-2.0。使用データのライセンスは [05](docs/design/05-commercial-licensing.md) と
`config/resource_policy.yaml` を参照。レポートには使用リソースのライセンス表が自動で付く。
