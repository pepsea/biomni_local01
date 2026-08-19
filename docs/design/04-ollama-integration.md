# 04. Ollama 統合と Biomni 側の落とし穴

Biomni は `source="Ollama"` を公式にサポートしているが、**そのまま A1 に渡すだけでは正しく動かない**。
以下は biomni 0.0.8 を実際にインストールし、モック Ollama サーバ（§4.10）で
HTTP リクエストを観測して確認した内容。数値はすべて実測値。

対象バージョン: **biomni 0.0.8（PyPI）**。GitHub main とは差がある
（main には `know_how_loader` があるが 0.0.8 には無い、など）。

## 4.0 まず依存が足りない

`biomni` の `pyproject.toml` が宣言している依存は `pydantic` / `langchain` / `python-dotenv` の 3 つだけだが、
実際には `from biomni.agent import A1` の時点で追加のパッケージが要る。

```
ModuleNotFoundError: No module named 'pandas'
ModuleNotFoundError: No module named 'langchain_openai'
```

さらに Ollama を使うには `langchain-ollama` が要る。これが無いと `biomni.llm.get_llm()` が
`ImportError` を投げるが、**A1 の import 自体は通ってしまう**ため、環境チェックで
`import biomni` だけを見ていると気付けない（notebook 01 で初めて落ちる）。

本アプリの `requirements.txt` では `pandas` / `langchain-openai` / `langchain-ollama` を
明示的にピン留めし、`biomni_hypo.config.missing_dependencies()` で不足を検出する。

```python
from biomni_hypo.config import install_hint, missing_dependencies

missing = missing_dependencies()          # find_spec で調べる（重い import をしない）
print(install_hint(missing))              # -> "pip install langchain-ollama pandas"
```

`notebooks/00`・`GET /api/health`・`scripts/setup_local.sh` がこれを使う。

## 4.1 致命的: Ollama 分岐で stop シーケンスが落ちる

`biomni/llm.py::get_llm()` は Anthropic / OpenAI / Custom には `stop_sequences` を渡すが、
**Ollama 分岐だけ渡していない**。

```python
elif source == "Ollama":
    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=model,
        temperature=temperature,
    )   # ← stop_sequences が渡されていない
```

A1 は `stop_sequences=["</execute>", "</solution>"]` で LLM を止め、コードを実行して
`<observation>` を差し込むことで ReAct ループを成立させている。stop が効かないと、
**モデルが `</execute>` の先に `<observation>` まで自分で書いてしまい、実行していない
コードの「実行結果」を捏造する。** 根拠の正しさを売りにする本アプリでは絶対に許容できない。

### 実測

モック Ollama で、実際に送信された `options` を観測した結果:

```python
get_llm("qwen3:14b", source="Ollama", stop_sequences=[...], base_url=...)
  → options = {'temperature': 0.7}                       # stop も num_ctx も無い

build_chat_ollama(settings, stop=AGENT_STOP_SEQUENCES)
  → options = {'temperature': 0.7, 'num_ctx': 32768,
               'num_predict': 4096, 'stop': ['</execute>', '</solution>']}
```

また `A1(...)` 構築直後の `agent.llm` は `stop=None, num_ctx=None, base_url=None` である。
この 3 つが埋まっていることが、本アプリの前提条件になる。

### 対策: A1 構築後に LLM を差し替える

`generate` ノードは `self.llm.invoke(...)` を**実行時に**参照するため、インスタンス属性の
差し替えで有効になる（グラフの再構築は不要）。

```python
# worker/agent_factory.py
from biomni.agent import A1
from langchain_ollama import ChatOllama

def build_agent(cfg) -> A1:
    agent = A1(
        path=cfg.data_path,
        llm=cfg.model,                      # 例: "qwen3:14b"
        source="Ollama",
        commercial_mode=True,               # §05: 本アプリは常に True
        use_tool_retriever=cfg.use_retriever,
        timeout_seconds=cfg.timeout_seconds,
        expected_data_lake_files=cfg.allowed_datasets,   # §4.4: 一括ダウンロード抑止
    )

    # ★ stop シーケンスと context 長を効かせるため LLM を差し替える
    agent.llm = ChatOllama(
        model=cfg.model,
        base_url=cfg.ollama_base_url,       # 既定 http://localhost:11434
        temperature=cfg.temperature,
        stop=["</execute>", "</solution>"], # ← 必須
        num_ctx=cfg.num_ctx,                # ← 必須（既定 2048 では即破綻）
        num_predict=cfg.num_predict,
        keep_alive="30m",                   # モデルのアンロード＆再ロードを防ぐ
    )
    return agent
```

**受け入れテスト**: 「1 + 1 を Python で計算して」のような自明なタスクを投げ、
LLM の生出力に `<observation>` が含まれていないことをアサートする。含まれていたら stop が効いていない。

## 4.2 `base_url` が Ollama に渡らない

`get_llm()` の Ollama 分岐は `base_url` 引数を無視する。`A1(base_url=...)` を指定しても効かない。
実測でも `get_llm(..., base_url="http://127.0.0.1:xxxxx")` の戻り値は `llm.base_url is None` で、
**`OLLAMA_HOST` 環境変数を設定しない限り向き先を変えられない**。

§4.1 の差し替えで `base_url` を明示すればこの問題は消える。
Docker Compose 構成では `http://ollama:11434` になる。

## 4.3 DB クエリツールが既定 LLM（Anthropic）を呼びに行く

`biomni/tool/database.py::_query_llm_for_api()` は、自然言語から API クエリを組み立てるために
**`default_config.llm` を使う**。これは `A1(llm=...)` とは別系統で、既定値は `claude-sonnet-4-5`。

```python
from biomni.config import default_config
model = default_config.llm      # ← A1 のコンストラクタ引数ではない
api_key = default_config.api_key
```

つまり `query_uniprot` / `query_kegg` / `query_ensembl` などを使った瞬間に
Anthropic API を呼ぼうとして失敗する（API キーが無いのでエラー、あれば**外部に送信される**）。

### 対策: 環境変数で default_config も Ollama に向ける

`BiomniConfig.__post_init__` は環境変数を読むので、**プロセス起動時に必ず設定する**。

```bash
BIOMNI_LLM=qwen3:14b
BIOMNI_SOURCE=Ollama
BIOMNI_TIMEOUT_SECONDS=600
BIOMNI_COMMERCIAL_MODE=true
BIOMNI_PATH=/app/data
LLM_SOURCE=Ollama          # llm.py の自動判定フォールバックにも効く
```

`default_config` は `biomni.config` の**モジュールインポート時に**生成されるため、
`import biomni` より前に環境変数が入っている必要がある。ワーカーの `main.py` 冒頭で
`load_dotenv()` を biomni の import より先に置く。起動時に
`assert default_config.llm == cfg.model and default_config.source == "Ollama"` で防御する。

## 4.4 データレイクの一括ダウンロード

`A1.__init__` は `expected_data_lake_files=None` のとき、`env_desc` に列挙された**全ファイルを
S3 から取得しようとする**（さらに benchmark ディレクトリも）。初回起動が数十 GB・数時間になる。

### 対策

- `expected_data_lake_files` に**明示的なリスト**を渡す（`[]` で完全スキップ）
- セットアップ用 CLI（`scripts/fetch_datasets.py`）で、許可リスト（§05）にあるデータセットのみを事前取得
- UI の設定画面でデータセットごとに「取得済み / 未取得 / サイズ」を表示し、必要なものだけ追加取得できるようにする

## 4.5 リソース検索プロンプトが巨大

`use_tool_retriever=True` のとき、`ToolRetriever.prompt_based_retrieval()` は
**全ツール（数百）＋全データレイク項目＋全ライブラリ（111）＋ノウハウ文書の説明文を 1 プロンプトに詰める**。
数万トークンになり、既定 `num_ctx=2048` のローカルモデルでは静かに切り捨てられて機能しない。

さらに応答は `TOOLS: [1, 5, 9]` 形式のテキストを正規表現で拾う実装のため、
指示追従の弱いモデルでは全カテゴリ空になり、その場合エージェントは何のツールも案内されずに走る。

### さらに深刻: リソース検索を切っても**システムプロンプト自体が巨大**

`use_tool_retriever=False` にしても、`configure()` が作るシステムプロンプトには
全ツールの説明・データレイク一覧・ライブラリ一覧が載る。実測値（biomni 0.0.8, `commercial_mode=True`）:

| モジュール構成 | ツール数 | プロンプト | ≒トークン | num_ctx=32768 の占有 |
| --- | ---: | ---: | ---: | ---: |
| 絞り込みなし (21) | 214 | 154,296 文字 | 38.6k | **118%（溢れる）** |
| `EXTENDED` (11) | 144 | 107,391 文字 | 26.8k | 82% |
| `DEFAULT` (5) | 75 | 66,008 文字 | 16.5k | 50% |
| `CORE` (3) | 47 | 38,115 文字 | 9.5k | 29% |

**絞り込まないと、システムプロンプトだけで `num_ctx=32768` を超える。**
会話が 1 往復も入らないので、エージェントは何もできない。

### 対策（優先順）

1. **モジュール単位で事前に絞る**（`agent_factory.py` のプリセット）。
   既定は `DEFAULT_TOOL_MODULES`（5 モジュール / 75 ツール / 約 16.5k トークン）。
   `AgentBundle.context_utilization` が 0.4 を超えたら警告を出す
2. `num_ctx` を上げる。`EXTENDED` を使うなら 65536 以上が要る
3. `use_tool_retriever=False` + 固定リソースセットで運用する。v1 の既定はこれ
4. リソース検索を有効にする場合、結果が全カテゴリ空だったら**既定リソースセットへフォールバック**する
   （現状は無言で空のまま進む）

## 4.6 `get_llm(config=...)` の属性名不一致

`get_llm()` は `model is None` のとき `config.llm_model` を参照するが、`BiomniConfig` の属性名は `llm`。
`AttributeError` になる経路が存在する（A1 は必ず model を渡すため通常は踏まない）。
**本アプリからは `get_llm(config=...)` を使わず、常に model を明示する**（§4.1 の差し替えで回避済み）。

## 4.7 モデル選定 — ローカルから読み込んで選ぶ

固定リストを持つのではなく、**`ollama pull` されているモデルを実際に読み込んで選択肢にする**
（`biomni_hypo/models.py`）。

```
GET /api/tags   -> pull 済みモデル・サイズ・量子化
POST /api/show  -> <arch>.context_length（モデルの最大 context）
        ↓
ResourcePolicy.check_model()  -> ライセンス判定（商用可否）
        ↓
ModelCatalog  -> 選択可 / 不可（理由付き） / 未取得
```

### ライセンス判定はファミリー単位

Ollama のモデル名はタグ付き（`qwen3:8b-instruct-q4_K_M`）なので、名前の完全一致では
実際に pull されているモデルを拾えない。**ファミリー名の前方一致**で判定する。

判定順（`config/resource_policy.yaml` の `models`）:

1. `deny_families` に前方一致 → 拒否（`allow` より強い）
2. `allow` に完全一致 → 許可（推奨タグ付き）
3. `allow_families` に前方一致 → 許可
4. どれにも当たらない → 拒否（既定拒否）

拒否が許可より強いのは、同じ接頭辞でライセンスが分かれるものがあるため。
`mistral-small` / `mistral-nemo` は Apache-2.0 だが **`mistral-large` は研究用途限定**、
`codestral` は非商用（MNPL）。`deepseek-r1` は MIT だが **`deepseek-coder-v2` は用途制限あり**。

| ファミリー | ライセンス | 商用 |
| --- | --- | --- |
| `qwen3` / `qwen2.5` / `qwq` | Apache-2.0 | ✅ 推奨 |
| `gpt-oss` | Apache-2.0 | ✅ |
| `deepseek-r1` / `deepseek-v3` | MIT | ✅ |
| `mistral` / `mixtral` / `devstral` | Apache-2.0 | ✅ |
| `phi3` / `phi4` | MIT | ✅ |
| `llama*` / `codellama` | Llama Community License | ❌ MAU 条項・命名条項 |
| `gemma*` / `medgemma` | Gemma 利用規約 | ❌ 利用制限条項 |
| `command-r` / `aya` | CC BY-NC | ❌ 非商用 |
| `mistral-large` / `pixtral-large` | Mistral Research License | ❌ 研究用途限定 |
| `codestral` | MNPL | ❌ 非商用 |
| `deepseek-coder-v2` | DeepSeek License | ❌ 用途制限 |
| 未知 | — | ❌ 既定拒否 |

**使えないモデルも理由付きで一覧に出す。** 黙って消すと「モデルが出てこない」という
問い合わせになるだけで、判断材料が残らない。

### num_ctx はモデルの上限に丸める

`num_ctx` にモデルの最大 context を超える値を渡すと、Ollama 側で黙って切り詰められ、
**システムプロンプトが欠けたまま走る**。`resolve_num_ctx()` が事前に丸め、
さらにシステムプロンプト（§4.5）を引いた残りが 8k トークンを切る場合は警告する。

### 選択の入口

| 入口 | 使い方 |
| --- | --- |
| CLI | `python scripts/list_models.py` / `--set <モデル名>` で `.env` を書き換え |
| API | `GET /api/models` → `POST /api/runs {"model": "..."}` |
| ノートブック | `00` と `04` の `apply_model_selection()` セル |

いずれも `biomni_hypo.models.apply_model_selection()` を通る。判定基準を 1 箇所に集める。

> `.env` を書き換えるときは `HYPO_MODEL` と **`BIOMNI_LLM` の両方**を変えること。
> biomni の DB クエリツールは A1 のコンストラクタ引数ではなく `default_config` を見る（§4.3）。
> `scripts/list_models.py --set` は両方直す。

### 役割別に別モデルを割り当てる

| 役割 | 要件 | 既定 |
| --- | --- | --- |
| Planner / Coder（A1 本体） | コード生成・長い context・タグ追従 | `qwen3:14b`, `num_ctx=32768` |
| Extractor（仮説 JSON 化） | スキーマ準拠 | 同モデル + `format=<json schema>`, `temperature=0.2` |
| Retriever（リソース選択） | 長い context | 同モデル（使う場合のみ） |

小さいマシンでは全部同じモデルでよいが、`temperature` は役割ごとに変える
（探索は 0.7、抽出は 0.2）。

## 4.8 パラメータ既定値

```jsonc
{
  "model": "qwen3:14b",
  "temperature": 0.7,          // 探索フェーズ。仮説の多様性を出す
  "num_ctx": 32768,            // ★ Ollama 既定 2048 は必ず上書きする
  "num_predict": 4096,
  "keep_alive": "30m",
  "timeout_seconds": 600,      // 1 コードブロックの実行上限
  "recursion_limit": 500,      // A1 既定。ローカルでは 60 程度に絞ることを推奨
  "use_tool_retriever": false, // v1 既定。§4.5
  "commercial_mode": true      // §05。変更不可
}
```

### モジュールプリセットと num_ctx の組み合わせ

| プリセット | 推奨 num_ctx | 会話に使える余白 |
| --- | ---: | ---: |
| `CORE` (3 モジュール) | 32768 | 約 23k トークン |
| `DEFAULT` (5 モジュール) | 32768 | 約 16k トークン |
| `EXTENDED` (11 モジュール) | 65536 | 約 38k トークン |

`num_ctx` を上げると KV キャッシュのメモリが増える。CPU 推論では速度にも効くので、
まず `CORE` で動かして、必要になったら広げるのが安全。

`recursion_limit` は A1 内で 500 固定。ローカル LLM だとループに嵌まると数時間走るため、
**ワーカー側でステップ数の上限とラン全体のウォールクロック上限を別途設ける**（既定 60 ステップ / 30 分）。

## 4.9 モック Ollama による検証（実機なしで配線を確かめる）

`biomni_hypo/mock_ollama.py` は Ollama の HTTP API（`/api/chat`, `/api/tags`, `/api/show`）を
最小限だけ実装したテスト用サーバ。応答を台本で与えられるので、**実機が無くても
「設定が実際にリクエストへ乗るか」と「A1 の ReAct ループが回るか」を確かめられる**。

```python
with MockOllama(replies=["<execute>\nprint(1)\n</execute>", "<solution>結論</solution>"]) as mock:
    settings.ollama_base_url = mock.base_url
    bundle = build_agent(settings, policy, tool_modules=CORE_TOOL_MODULES)
    result = TracingRunner(bundle).run("質問")
    assert mock.last_options()["stop"] == AGENT_STOP_SEQUENCES
```

`tests/test_integration_biomni.py` がこれを使って、実物の biomni に対して次を固定している。

- `build_chat_ollama()` が stop / num_ctx を送ること
- `biomni.llm.get_llm()` が **送らない**こと（直ったらテストが落ちて気付ける）
- 拒否ツールがシステムプロンプトから消えること
- 本物の `run_python_repl` に届く前にポリシーガードが割り込むこと
- `run_hypothesis()` が最後まで通ること

**検証できないのはモデルの挙動そのもの**（指示に従うか、コードを書けるか）。
それは実機で `notebooks/01` と `notebooks/04` を回して見る。

## 4.10 将来の差し替え

`ChatOllama` を組み立てている箇所は `agent_factory.py` の 1 関数に閉じる。
vLLM / SGLang（OpenAI 互換）に移す場合は `source="Custom"` + `base_url` で置き換えられる。
クラウド LLM を選択肢に加える場合も同じ関数の分岐で済む設計にしておく。
