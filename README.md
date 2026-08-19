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
| コアパッケージ (`biomni_hypo/`) | ✅ 実装済み |
| 検証ノートブック (`notebooks/`) | ✅ 5 本 |
| API + SSE (`backend/`) | ✅ 実装済み・実サーバで動作確認 |
| テスト | ✅ **188 件**（うち 12 件は実物の biomni に対する統合テスト） |
| モデル選択 | ✅ ローカルの Ollama を読み込んで選択（ライセンス判定つき） |
| 質問入力 | ✅ 構造化入力・テンプレート・入力検査・プロンプト確認 |
| Web UI | ✅ 依存なしの 1 ファイル（`/`）。React 版は未着手（設計は [07](docs/design/07-ui-design.md)） |

**検証済み**: biomni 0.0.8 を実際にインストールし、モック Ollama サーバを相手に
A1 の構築・ReAct ループ・ポリシーガード・パイプライン全体が動くことを確認した。

**未検証**: 実モデルの挙動そのもの（指示に従うか、まともなコードを書けるか）。
これは実機の Ollama でしか分からない → [09 §9.6](docs/design/09-implementation.md)

## クイックスタート

### いちばん簡単

```bash
bash scripts/setup_local.sh          # 最小構成（テストが通るところまで）
bash scripts/setup_local.sh --full   # biomni + Ollama モデル + データセット
```

### 手動でやる場合

**1. まずテストだけ動かす（依存 4 つ、Ollama 不要）**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pydantic pyyaml requests pytest
pytest -q                       # 96 passed（biomni 統合テストはスキップされる）
```

根拠の抽出・検証・レポート生成のロジックはこれだけで確認できる。

**2. 全部入れる**

```bash
pip install -r requirements.txt
cp .env.example .env
pytest -q                       # 108 passed
```

> `pip install biomni` だけでは `from biomni.agent import A1` が落ちる。
> biomni 0.0.8 は `pandas` と `langchain-openai` を依存として宣言していないため。
> `requirements.txt` で明示的に入れている。

**3. Ollama とデータセット**

```bash
curl -fsSL https://ollama.com/install.sh | sh   # または brew install ollama
ollama serve &
ollama pull qwen3:14b
python scripts/fetch_datasets.py --only gwas_catalog.pkl gene_info.parquet
```

**4. モデルを選ぶ**

ローカルに pull 済みのモデルを読み込んで、商用利用ポリシーで判定する。

```bash
python scripts/list_models.py          # 一覧（make models）
```

```
★ qwen3:14b                   9.3GB   40,960 Apache-2.0
★ qwen3:8b-instruct-q4_K_M    5.2GB   40,960 Apache-2.0
✓ deepseek-r1:7b              4.7GB  131,072 MIT         思考トークンが長い。stop 制御を要検証
✕ llama3.1:8b                 4.9GB        - Llama Community License  MAU 条項と命名条項があるため既定で不可
✕ gemma3:12b                  8.1GB        - Gemma Terms of Use       利用制限条項があるため既定で不可
… qwen3:32b                       -        - Apache-2.0  未取得: ollama pull qwen3:32b

★ 推奨 / ✓ 選択可 / ✕ ライセンス不可 / … 未取得
```

```bash
python scripts/list_models.py --set qwen3:8b   # .env の既定を変える
```

タグ違い（`qwen3:8b-instruct-q4_K_M` など）もファミリー名で判定するので、
許可リストに無い名前でも正しく拾う。**使えないモデルも理由付きで表示する**（黙って隠さない）。
`num_ctx` はモデルの上限に自動で丸められる。

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

ブラウザで **http://localhost:8000** を開くと入力画面が出ます。

```
モード  ● 仮説生成   ○ 根拠検証   ○ データ解釈
例から始める  [治療抵抗性の機序を探す ▾]

課題   [トリプルネガティブ乳がんで PARP 阻害剤耐性を規定する因子は？]
生物種 [ヒト]   対象 [TNBC、オラパリブ投与下]
注目   [BRCA1, BRCA2, 相同組換え修復]

           [ 仮説を構築する ]  [ プロンプトを確認 ]
```

`text` 以外は任意ですが、埋めるほど探索が安定します。埋まっていない項目は指摘が出ます。
**「プロンプトを確認」で、エージェントに何を投げるかを実行前に見られます。**

ターミナルから使う場合:

```bash
python scripts/ask.py                                    # 対話入力
python scripts/ask.py "TNBC の PARP 阻害剤耐性は？" --organism ヒト
python scripts/ask.py --template resistance --dry-run    # プロンプトだけ確認
```

```bash
curl -s localhost:8000/api/models                   # ローカルのモデル一覧
curl -X POST localhost:8000/api/runs -H 'content-type: application/json' \
  -d '{"question": "TNBC で PARP 阻害剤耐性を規定する因子は？", "model": "qwen3:14b"}'
curl -N localhost:8000/api/runs/<run_id>/events     # SSE でトレースが流れる
curl -s localhost:8000/api/runs/<run_id>/report     # Markdown レポート
```

Docker なら `docker compose up`（ollama + api）。

## 構成

```
biomni_hypo/     共有コアパッケージ ← ノートブックも Web アプリもここを呼ぶ
  question.py    調べたいことの入力・検査・プロンプト組み立て
  models.py      ローカルモデルの探索・ライセンス判定・選択
notebooks/       検証ハーネス（ロジックは書かない。テストで強制）
backend/app/     FastAPI + SSE + ラン実行ワーカー（子プロセス）+ 最小 UI
config/          resource_policy.yaml（商用限定・既定拒否）
scripts/         質問の実行(ask)・モデル一覧・データセット取得・セットアップ
tests/           188 件。うち 176 件は外部サービス不要
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
| [10-question-input](docs/design/10-question-input.md) | **調べたいことの入力**（構造化・検査・プロンプト組み立て） |

## 設計の要点

1. **`A1.go_stream()` を使わない。** 整形済み文字列しか返さず根拠抽出に必要な情報が失われるため、
   A1 内部の LangGraph を直接ストリームして構造化する（`biomni_hypo/tracing.py`）。
2. **仮説の生成と JSON 化を分離する。** トレースから機械抽出した根拠候補を渡し、
   **その ID からしか選べない**制約下で仮説を書かせる（`biomni_hypo/extractor.py`）。
3. **引用は実在検証を通してから表示する。** 「コードで触れていないのに引用された」ものは
   幻覚として隔離する（`biomni_hypo/verifier.py`）。
4. **商用利用は `commercial_mode=True` だけでは足りない。** Biomni の同フラグはデータセットを
   絞るがツールは絞らないため、独自ポリシーで既定拒否を敷く（`biomni_hypo/policy.py`）。

## 既知の落とし穴（biomni 0.0.8 で実測）

`biomni_hypo/agent_factory.py` にすべて封じ込めてあり、`tests/test_integration_biomni.py` が
実物に対して固定している。詳細は [04](docs/design/04-ollama-integration.md)。

| # | 問題 | 対策 |
| --- | --- | --- |
| §4.0 | `pandas` / `langchain-openai` が依存宣言に無く、A1 を import できない | `requirements.txt` で明示 |
| §4.1 | `get_llm()` の **Ollama 分岐だけ `stop_sequences` を渡していない** → モデルが実行結果を捏造する。実測: 送信 options が `{'temperature': 0.7}` のみ | `agent.llm` を `ChatOllama(stop=...)` に差し替え |
| §4.2 | 同分岐は `base_url` も無視する（実測: `llm.base_url is None`。`OLLAMA_HOST` でしか変えられない） | 同上 |
| §4.3 | `database.py` は `default_config.llm`（既定 Claude）を使う → DB ツールが外部 API を叩く | `apply_biomni_env()` を import 前に実行 |
| §4.4 | `A1.__init__` がデータレイクを一括ダウンロードする | `expected_data_lake_files` を明示 |
| §4.5 | **システムプロンプトだけで `num_ctx=32768` を超える**（絞り込みなしで 38.6k トークン） | モジュールプリセットで既定 16.5k に。占有率が 40% を超えたら警告 |

## ライセンス

Apache-2.0。使用データのライセンスは [05](docs/design/05-commercial-licensing.md) と
`config/resource_policy.yaml` を参照。レポートには使用リソースのライセンス表が自動で付く。
