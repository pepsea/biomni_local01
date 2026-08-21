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
| 設計 (`docs/design/`) | ✅ 01〜21 |
| コアパッケージ (`biomni_hypo/`) | ✅ 実装済み |
| 検証ノートブック (`notebooks/`) | ✅ 5 本 |
| API + SSE (`backend/`) | ✅ 実装済み・実サーバで動作確認 |
| テスト | ✅ **336 件**（うち 16 件は実物の biomni に対する統合テスト） |
| モデル選択 | ✅ ローカルの Ollama を読み込んで選択（ライセンス判定つき） |
| 質問入力 | ✅ 構造化入力・テンプレート・入力検査・プロンプト確認 |
| Web UI | ✅ 依存なしの静的ページ。`/` に**解析の設計**・回答・**論点**・根拠・情報源・トレース、`/history` に条件つき検索 |
| リアルタイム出力 | ✅ トークン単位の実況（biomni 無改変） |
| LLM プロバイダ | ✅ Ollama（ローカル）と Claude API を**実行ごとに**選択 |
| 調査履歴 | ✅ 条件込みで検索・絞り込み・再表示・削除 |
| 実行の停止 | ✅ プロセスグループごと停止（孫プロセスまで） |

**検証済み**: biomni 0.0.8 を実際にインストールし、モック Ollama サーバを相手に
A1 の構築・ReAct ループ・ポリシーガード・パイプライン全体が動くことを確認した。

**未検証**: 実モデルの挙動そのもの（指示に従うか、まともなコードを書けるか）。
これは実機の Ollama でしか分からない → [09 §9.6](docs/design/09-implementation.md)

## クイックスタート

> **別の Linux マシンに入れる場合は [docs/INSTALL-linux.md](docs/INSTALL-linux.md)** に
> まっさらな状態からの手順（Docker 導入・GPU・公開範囲・つまずいたとき）をまとめてあります。

### Linux に常設する（推奨）

```bash
make service-install
```

Docker + systemd で常設します。マシンを再起動しても自動で復帰し、
`systemctl` から扱えます。詳細は [13-linux-deployment.md](docs/design/13-linux-deployment.md)。

| コマンド | 用途 |
| --- | --- |
| `make service-status` | 状態 |
| `make service-logs` | ログ |
| `make service-update` | `git pull` して入れ替え |
| `make service-uninstall` | 取り外す（データは残る） |

ポートを変えるには `.env` に `APP_PORT=9000` と書いて `make docker-rebuild`。
外部に出したくないなら `APP_BIND=127.0.0.1` も足します。

### Ollama と Claude を両方使えるようにする（推奨）

```bash
bash scripts/set-provider.sh both --key sk-ant-... --port 8003
make docker-rebuild        # Docker の場合。非 Docker なら bash scripts/start.sh
```

これで画面のモデル選択に「ローカル (Ollama)」と「クラウド (Claude API)」が並び、
**実行ごとに切り替えられます**。`.env` を書き換え直す必要はありません。

既定を Claude にしたいときは `--default claude`、モデルを指定するときは
`--model qwen3:14b --claude-model claude-sonnet-5`。

`ANTHROPIC_API_KEY` が環境にあっても、**Ollama を選んだ実行では biomni の
`BIOMNI_SOURCE` が `Ollama` に張り替わります**。DB クエリツールだけが黙って
Anthropic を呼ぶ事故は起きません（[15](docs/design/15-provider-switching.md) §15.2）。

#### 片方だけ使う

```bash
bash scripts/set-provider.sh claude --key sk-ant-...   # Claude のみ
bash scripts/set-provider.sh ollama --model qwen3:14b  # Ollama のみ
```

Claude のみにすると `COMPOSE_PROFILES` が空になり、
**Ollama コンテナは起動せず 9GB のモデル取得も走りません**。

いずれの場合も `HYPO_PROVIDER` / `HYPO_MODEL` / `BIOMNI_SOURCE` /
`COMPOSE_PROFILES` の 4 つを揃えて書き換えます（手で書くと食い違いやすい箇所です）。

> オフラインモード（`HYPO_OFFLINE_MODE=true`）はクラウドのモデルと併用できません。
> クラウドを選ぶと画面側でもチェックが外れて無効になります。

### 過去の調査を探す

「履歴」タブから、**条件込みで**検索できます。

- フリーワード（質問文・回答・仮説・使ったリソース名・PMID などの証拠識別子が対象）
- プロバイダ / モデル / モード / 対象生物 / 状態 での絞り込み（選択肢は実データから生成）
- カードには条件（モデル・モード・対象生物・文脈・注目点・オフライン）と
  結果（仮説数・検証済/失敗の証拠数・手順数・所要時間）を併記
- クリックすると回答・仮説・集めた情報・実行トレースをそのまま復元

詳細は [14-search-and-history.md](docs/design/14-search-and-history.md)。

### 「biomni を読み込めません」と出るとき

```bash
make app-check
```

Docker ならコンテナの中で、そうでなければ手元の Python で、**実際に import して**
理由を出します。`/api/health` の `biomni` にも版と例外が載ります。

### Ollama は起動しているのに「未接続」と出るとき

```bash
make ollama-check
```

ホスト → Ollama、`.env` の設定、コンテナ → Ollama の 3 か所を実際に `curl` で叩いて、
どこで切れているかと直し方を出します。よくある原因は
[17-ollama-connectivity.md](docs/design/17-ollama-connectivity.md) にまとめてあります
（コンテナの `localhost` はホストではない / Ollama が 127.0.0.1 しか待ち受けていない、など）。

### ポートが衝突するとき

```
[Errno 48] error while attempting to bind on address ('0.0.0.0', 8003): address already in use
ports are not available: ... bind: address already in use
```

`bash scripts/start.sh` は起動前にポートを確認し、掴んでいるプロセスを名指しして止まります。
既にこのアプリ自身が同じポートで動いている場合は、その旨だけ伝えて何も起動しません
（二重起動して片方が落ちる、という状態を作らないため）。

```bash
lsof -nP -iTCP:8003 -sTCP:LISTEN   # 何が掴んでいるか
make docker-down                   # Docker のコンテナなら
bash scripts/start.sh --port 8004  # 別のポートで動かす
```

ホストに直接入れた Ollama が 11434 を使っているのに、コンテナ版も立てようとした場合です。

```bash
make docker-check                    # 誰が使っているかまで出る
bash scripts/use-host-ollama.sh      # ホストの Ollama をそのまま使う（おすすめ）
make docker-rebuild
```

詳細は [12-docker.md §12.10](docs/design/12-docker.md)。

### 手元で Docker だけ動かす

Python の環境で悩みたくないならこれが確実です。イメージの中に Python は 1 つだけです。

```bash
make docker-up          # ビルドして常駐起動
```

http://localhost:8000 が開きます。初回はモデル取得（約 9GB）に時間がかかるので
`make docker-logs` で進行を見てください。`restart: unless-stopped` なので、
`make docker-down` するまで動き続けます（マシン再起動後も復帰します）。

詳細は [12-docker.md](docs/design/12-docker.md)。

### ホストに直接入れる

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

> **`pip install biomni` だけでは動かない。** biomni 0.0.8 の `pyproject.toml` は
> `pydantic` / `langchain` / `python-dotenv` しか宣言しておらず、`pandas` と
> `langchain-openai` が無いと `from biomni.agent import A1` が落ちる。
> Ollama を使うには `langchain-ollama` も要る。`requirements.txt` で全部入れている。
>
> 何が足りないかは `notebooks/00`、`GET /api/health`、`bash scripts/setup_local.sh` の
> いずれでも確認できる（`pip install ...` のコマンドまで出る）。

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
make up                      # または: bash scripts/start.sh
```

起動前に環境を確認して、足りないものを具体的に指摘します。

```
== Python 環境
  ✓ ./.venv を使います
== 依存パッケージ
  ✓ API サーバの依存 OK
  ✓ エージェントの依存 OK
== LLM
  ✓ 使えるモデル 2 件: qwen3:14b, qwen3:8b
      既定: qwen3:14b（ローカル）
== 起動
  ブラウザで  http://localhost:8000  を開いてください（停止は Ctrl-C）
```

| コマンド | 用途 |
| --- | --- |
| `make up` | 起動（環境確認つき）。ふだんはこれ |
| `make check-env` | 起動せず環境だけ確認（`scripts/start.sh --check`） |
| `make doctor` | **どの Python を使っているか診断**（依存が入らないとき） |
| `bash scripts/start.sh --port 9000` | ポートを変える（`.env` の `APP_PORT` でも可） |
| `bash scripts/start.sh --reload` | コード変更を自動反映（開発用） |
| `make docker-up` | **Docker で常駐起動**（Ollama ごと。[12](docs/design/12-docker.md)） |

**Ollama も Claude API も無くてもサーバは起動します**（モデルが選べないだけ）。
`make check-env` で何が足りないか分かります。

### 「pip install したのに ModuleNotFoundError」

インストール先の Python と実行している Python が違う、が原因のほぼ全てです。

```bash
make doctor
```

見つかった Python ごとに「何が入っているか」と「この Python に入れるコマンド」が出ます。

```
  /home/you/projects/jupyter/.venv/bin/python  (Python 3.12.3 / 仮想環境)
      ✓ アプリ本体      3/3
      · エージェント     5/7   足りない: pandas, tqdm
      → この Python に入れるなら:
        /home/you/projects/jupyter/.venv/bin/python -m pip install pandas tqdm
```

Jupyter のセルからなら `%pip install <パッケージ>` を使ってください（カーネルの Python に入ります）。

ブラウザで **http://localhost:8000** を開きます。

```
モード  ● 仮説生成   ○ 根拠検証   ○ データ解釈
例から始める  [治療抵抗性の機序を探す ▾]

課題   [トリプルネガティブ乳がんで PARP 阻害剤耐性を規定する因子は？]
生物種 [ヒト]   対象 [TNBC、オラパリブ投与下]
注目   [BRCA1, BRCA2, 相同組換え修復]

           [ 調べる ]  [ プロンプトを確認 ]
```

実行すると 4 つのタブに結果が出ます。

| タブ | 内容 |
| --- | --- |
| **回答** | 質問への直接の回答と、その根拠 |
| **仮説** | 検証可能な仮説・根拠・検証プラン |
| **集めた情報** | 使用したデータとライセンス、引用した文献・DB レコード |
| **実行トレース** | **生成中のトークンをリアルタイム表示** + 手順（コード・出力を展開可） |

根拠のチップをクリックすると、実行結果からの抜粋・それを出したコード・由来ステップが開きます。

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
  models.py      モデルの探索（Ollama / Claude API）・ライセンス判定・選択
notebooks/       検証ハーネス（ロジックは書かない。テストで強制）
backend/app/     FastAPI + SSE + ラン実行ワーカー（子プロセス）+ 最小 UI
  store.py       ラン保存と検索（条件も列に射影する）
config/          resource_policy.yaml（商用限定・既定拒否）
scripts/         質問の実行(ask)・モデル一覧・データセット取得・セットアップ
tests/           336 件。うち 320 件は外部サービス不要
docs/design/     設計書
```

## ドキュメント

| ドキュメント | 内容 |
| --- | --- |
| [INSTALL-linux](docs/INSTALL-linux.md) | **別の Linux に導入する手順**（Docker / venv・GPU・公開範囲） |

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
| [11-realtime-and-providers](docs/design/11-realtime-and-providers.md) | **リアルタイム出力**とプロバイダ選択（Ollama / Claude API） |
| [12-docker](docs/design/12-docker.md) | **Docker で常駐**させる |
| [13-linux-deployment](docs/design/13-linux-deployment.md) | **Linux に常設**する（systemd） |
| [14-search-and-history](docs/design/14-search-and-history.md) | **調査履歴の検索**（条件込み） |
| [15-provider-switching](docs/design/15-provider-switching.md) | **Ollama と Claude を両方**選べるようにする |
| [16-parsing-errors](docs/design/16-parsing-errors.md) | **タグ無し応答**（"there are no tags..."）の原因と対処 |
| [17-ollama-connectivity](docs/design/17-ollama-connectivity.md) | **Ollama に繋がらない**ときの切り分け |
| [18-answer-reasoning](docs/design/18-answer-reasoning.md) | **最終回答に論点を持たせる**（biomni 既定は採点用の短答） |
| [19-analysis-plan](docs/design/19-analysis-plan.md) | **解析の設計**（biomni が最初に立てる計画）を見えるようにする |
| [20-lazy-tool-dependencies](docs/design/20-lazy-tool-dependencies.md) | **関数内 import のツール依存**（`No module named 'pymed'`） |
| [21-web-run-fixes](docs/design/21-web-run-fixes.md) | Web 経由のランで踏んだ不具合（構造化入力・Claude の temperature・Ollama・履歴） |

## 設計の要点

1. **`A1.go_stream()` を使わない。** 整形済み文字列しか返さず根拠抽出に必要な情報が失われるため、
   A1 内部の LangGraph を直接ストリームして構造化する（`biomni_hypo/tracing.py`）。
2. **仮説の生成と JSON 化を分離する。** トレースから機械抽出した根拠候補を渡し、
   **その ID からしか選べない**制約下で仮説を書かせる（`biomni_hypo/extractor.py`）。
3. **引用は実在検証を通してから表示する。** 「コードで触れていないのに引用された」ものは
   幻覚として隔離する（`biomni_hypo/verifier.py`）。
4. **最終回答は結論だけにしない。** biomni の `<solution>` は既定ではベンチマーク採点用の
   短答（唯一の例が 1 文字の `A`）なので、結論に至った論点を抽出フェーズで組み立て直す。
   反証と「分からなかったこと」を必ず持ち越す（`docs/design/18`）。
5. **商用利用は `commercial_mode=True` だけでは足りない。** Biomni の同フラグはデータセットを
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
| [§20](docs/design/20-lazy-tool-dependencies.md) | ツールが依存を**関数の中で** import する（`query_pubmed` → `pymed`）。モジュール検査を素通りし、呼ばれた瞬間に落ちる。文献を引けなくなったエージェントは自分の記憶で書き始める | AST で関数内 import を読み、依存が無いツールは案内しない。`pymed` / `arxiv` を追加 |
| [§16](docs/design/16-parsing-errors.md) | タグの無い応答を 2 回で打ち切る。画面には英文の叱責が `think` として出るだけ | `PARSING_ERROR` に分類し、原因と対処を日本語で表示。プロンプト末尾に出力形式を再掲 |

## ライセンス

Apache-2.0。使用データのライセンスは [05](docs/design/05-commercial-licensing.md) と
`config/resource_policy.yaml` を参照。レポートには使用リソースのライセンス表が自動で付く。
