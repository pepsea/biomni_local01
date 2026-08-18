# 09. 実装の構成 — ノートブック検証から Web アプリまで

設計（01〜08）に対する実装のマップ。**「ノートブックで検証したものがそのまま本番になる」**
ことを構造で保証するのが狙い。

## 9.1 レイヤ構成

```
                    ┌─────────────────────────────────┐
notebooks/  ───────>│                                 │
                    │   biomni_hypo/  (共有コア)       │──> Ollama
backend/app/ ──────>│   ここにロジックを集約する        │──> Biomni A1
                    └─────────────────────────────────┘
```

ノートブックと Web アプリは**同じ関数を呼ぶ**。ノートブックにロジックを書かない。
これは方針ではなくテストで強制する（`tests/test_notebooks.py::test_notebooks_import_the_shared_package_not_reimplement_it`
がノートブック内の `def` / `class` を検出して落とす）。

## 9.2 コアパッケージ `biomni_hypo/`

| モジュール | 責務 | 対応する設計 |
| --- | --- | --- |
| `schemas.py` | ドメインモデル（Run / Step / Hypothesis / Evidence / Resource） | 03 §3.1 |
| `config.py` | 設定と、**biomni の環境変数を import 前に適用する** `apply_biomni_env()` | 04 §4.3 |
| `policy.py` | 商用限定のリソースポリシー。既定拒否 | 05 §5.2 |
| `llm.py` | `ChatOllama` の構築（stop / num_ctx / base_url）と疎通確認 | 04 §4.1, §4.2 |
| `agent_factory.py` | A1 の構築。**Ollama の落とし穴をすべてここに封じる** | 04 全体 |
| `guard.py` | コード実行直前のポリシー検査（最後の砦） | 05 §5.2 強制ポイント 3 |
| `tracing.py` | A1 の LangGraph を直接ストリームして Step に構造化 | 02 §2.3 |
| `citations.py` | observation からの識別子抽出 | 03 §3.5 |
| `extractor.py` | トレース → 仮説 JSON（候補 eid からしか選ばせない） | 03 §3.3 |
| `verifier.py` | 引用の実在検証と包含チェック（C ⊆ B） | 03 §3.4 |
| `pipeline.py` | ラン 1 本の共通エントリ `run_hypothesis()` | 02 §2.4 |
| `report.py` | Markdown レポート（ライセンス表記を自動生成） | 03 §3.6, 05 §5.3 |
| `fixtures.py` | オフライン検証用のフェイク（本番からは import しない） | — |
| `mock_ollama.py` | 検証用のモック Ollama サーバ（本番からは import しない） | 04 §4.9 |

### 依存の重さを分けてある

`pydantic` / `pyyaml` / `requests` だけで、`citations` / `policy` / `extractor` /
`verifier` / `report` / `pipeline`（フェイク注入時）が動く。
`biomni` と `langchain-ollama` は **遅延 import** にしてあるので、
重い環境を作る前にノートブック 03 とテスト全件が回る。

## 9.3 ノートブック

| ノートブック | 目的 | Ollama | biomni |
| --- | --- | --- | --- |
| `00_environment_check` | 5 項目のチェック。ここが緑になるまで先に進まない | 要 | 要 |
| `01_ollama_stop_sequence` | **最重要**。stop シーケンスが効くかを A/B で確認（AC-1） | 要 | 要 |
| `02_agent_tracing` | A1 構築とトレース構造化。フォールバックあり | 要 | 要 |
| `03_evidence_extraction` | 抽出→検証→レポート。**不要**（フィクスチャで完走） | 任意 | 不要 |
| `04_end_to_end` | 本番と同じ `run_hypothesis()` を回して受け入れ基準を測る | 要 | 要 |

`01` を最初に置いているのは、ここが通らなければ他がすべて無意味になるため。
`biomni.llm.get_llm()` の Ollama 分岐は `stop_sequences` を渡しておらず、
そのままだと**モデルが実行していないコードの「実行結果」を捏造する**。
ノートブック 01 は `get_llm()` 版と `build_chat_ollama()` 版を並べて、差を目で見る作りにしてある。

## 9.4 Web アプリ

| ファイル | 責務 |
| --- | --- |
| `backend/app/main.py` | FastAPI。HTTP と SSE の層に徹し、ドメインロジックを持たない |
| `backend/app/worker.py` | ラン 1 本ごとに子プロセスを起こし `run_hypothesis()` を呼ぶ |
| `backend/app/store.py` | sqlite3 に RunResult の JSON とイベントを保存 |

### 設計との差分（意図的なもの）

| 設計 (02) | 実装 (v1) | 理由 |
| --- | --- | --- |
| SQLModel + SQLite | 標準ライブラリ `sqlite3` + JSON カラム | 依存を増やさない。スキーマが固まったら移行する |
| ウォームプールの常駐ワーカー | ラン 1 本ごとの子プロセス | 隔離と状態リセットが確実。A1 構築コストは次の課題 |
| React フロントエンド | 未実装 | API と SSE が先。UI 設計は 07 |

子プロセス方式にしているのは、`run_python_repl` が **LLM の生成コードをサンドボックスなしで
`exec` する**ため（02 §2.6）。API サーバと同じプロセスで走らせない。

## 9.5 テスト

```
pytest -q     # 96 件
```

| ファイル | 守っているもの |
| --- | --- |
| `test_citations.py` | 識別子抽出。抜粋が実テキスト由来であること。ゲート付きパターン |
| `test_policy.py` | 既定拒否。KEGG などのツール拒否。コード静的検査 |
| `test_extractor.py` | **未知 eid が仮説に入らないこと**。LLM が書いた抜粋を採用しないこと |
| `test_verifier.py` | 包含チェック。ネットワーク障害を捏造と誤判定しないこと |
| `test_tracing.py` | ステップ分類。**observation の自己生成検知（AC-1）**。ガードの復元 |
| `test_pipeline.py` | エンドツーエンド。抽出失敗でランを落とさないこと。レポート内容 |
| `test_api.py` | ポリシー違反モデルの拒否。SSE の再送 |
| `test_notebooks.py` | ノートブックの構文。出力を含めないこと。ロジックを書かないこと |
| `test_integration_biomni.py` | **実物の biomni** に対する検証。biomni 未インストールならスキップ |

外部依存（Ollama / biomni / ネットワーク）は**すべて注入点を用意**してあるので、
ユニットテストは 1 秒未満で完走する。注入点は本番でも意味のある拡張点になっている
（`EvidenceVerifier(pmid_checker=...)`, `HypothesisExtractor(llm=...)`,
`TracingRunner(guard_module=...)`）。

`test_integration_biomni.py` だけは実物の biomni を使う（Ollama はモックで代替、約 8 秒）。
`pytest.importorskip` があるので、biomni の無い環境では自動でスキップされる。

## 9.6 検証済みのこと / まだ検証していないこと

### 検証済み（`pytest -q` で自動化されている）

biomni 0.0.8 を実際にインストールし、モック Ollama サーバ（04 §4.9）を相手に確認した。

| 項目 | どこで |
| --- | --- |
| `A1` がデータレイクをダウンロードせずに構築できる | `test_a1_builds_without_downloading_the_data_lake` |
| **`biomni.llm.get_llm()` が stop も num_ctx も送らない**（不具合の実在） | `test_biomni_get_llm_drops_stop_sequences` |
| `build_chat_ollama()` が stop / num_ctx を実際に HTTP へ乗せる | `test_our_builder_sends_stop_and_num_ctx` |
| `build_agent()` 後の `agent.llm` に stop / num_ctx / base_url が入る | `test_build_agent_installs_stop_sequences` |
| 拒否ツールがシステムプロンプトから消える | `test_denied_tools_disappear_from_the_system_prompt` |
| 絞り込まないとプロンプトが num_ctx を溢れさせる | `test_module_presets_control_the_prompt_size` |
| **本物の A1 の LangGraph が回り、ステップが正しく分類される** | `test_real_graph_produces_classified_steps` |
| 本物の実行経路でポリシーガードが割り込む | `test_policy_guard_intercepts_the_real_execution_path` |
| `run_hypothesis()` が最後まで通る | `test_full_pipeline_against_mock_ollama` |

### まだ検証していない = **モデルの挙動そのもの**

モックは台本どおりに応答するだけなので、次は実機でしか分からない。

1. `notebooks/00` — 5 項目すべて ✅ になるか
2. `notebooks/01` — **stop あり版で `observation を自己生成した: False` が安定して出るか**
   （配線は検証済み。残る問いは「モデルが `</execute>` を出力するか」）
3. `notebooks/02` — 実モデルが `<execute>` にまともなコードを書けるか
4. `notebooks/04` — 受け入れ基準セルの ✅/❌、および所要時間
5. `make api` → `POST /api/runs` → SSE が流れるか

2 で ❌ が出たら、そこから先の出力はすべて信用できない。まずモデルを大きくすること。

## 9.7 次の実装ステップ

1. **React フロントエンド**（07 の画面設計）。API と SSE は既にある
2. **ユーザーデータのアップロード**（`POST /api/uploads` → `agent.add_data()`）
3. **A1 のウォームプール**。ラン開始のレイテンシが構築コストに支配されている
4. **評価セット**（08 §8.3）。答えが既知の質問 20 件を固定し、モデル変更のたびに回す
5. **ライセンススキャン**を CI に追加（`pip-licenses` で GPL / AGPL / 非商用を検出）
