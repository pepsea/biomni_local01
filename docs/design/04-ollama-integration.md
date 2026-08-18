# 04. Ollama 統合と Biomni 側の落とし穴

Biomni は `source="Ollama"` を公式にサポートしているが、**そのまま A1 に渡すだけでは正しく動かない**。
`biomni/llm.py` と `biomni/agent/a1.py` を読んで確認した具体的な問題と対策を以下に示す。
（対象バージョン: biomni 0.0.8 / GitHub main）

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
Ollama を別ホスト／別ポートで動かす場合は、§4.1 の差し替えで `base_url` を明示するか、
`OLLAMA_HOST` 環境変数を設定する。Docker Compose 構成では `http://ollama:11434` になる。

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

### 対策（優先順）

1. `num_ctx` を 32768 以上にする（`qwen3:14b` や `gpt-oss:20b` なら現実的）
2. **モジュール単位で事前に絞る**: `agent.module2api` から本アプリで使うモジュール
   （`literature`, `database`, `genomics`, `genetics`, `pharmacology`, `systems_biology`, `support_tools` など）
   だけを残してから `ToolRegistry` を作り直す。これだけでプロンプトが 1/3 以下になる
3. 検索結果が全カテゴリ空だった場合に**既定リソースセットへフォールバック**する（現状は無言で空のまま進む）
4. それでも不安定なら `use_tool_retriever=False` + 固定リソースセットで運用する。
   v1 の既定はこれ（**確実に動く構成から始める**）

## 4.6 `get_llm(config=...)` の属性名不一致

`get_llm()` は `model is None` のとき `config.llm_model` を参照するが、`BiomniConfig` の属性名は `llm`。
`AttributeError` になる経路が存在する（A1 は必ず model を渡すため通常は踏まない）。
**本アプリからは `get_llm(config=...)` を使わず、常に model を明示する**（§4.1 の差し替えで回避済み）。

## 4.7 モデル選定

| モデル | ライセンス | 用途 | 所感 |
| --- | --- | --- | --- |
| `qwen3:14b` / `qwen3:32b` | Apache-2.0 | **推奨: 計画・コード生成** | コード生成とタグ追従が安定。14b で 24GB VRAM 目安 |
| `gpt-oss:20b` | Apache-2.0 | 計画・コード生成 | `llm.py` が `gpt-oss` プレフィクスを Ollama と自動判定する |
| `qwen2.5-coder:14b` | Apache-2.0 | コード生成特化 | `<execute>` の中身の質は高いが計画力は劣る |
| `deepseek-r1:14b` | MIT | 推論重視 | 思考トークンが長く、stop 制御と相性を要検証 |
| `gemma3` 系 | Gemma 利用規約 | — | 商用可だが利用制限条項あり。§05 の方針では非推奨 |
| `llama3.1:8b` 等 | Llama Community License | — | 商用可だが MAU 条項・命名条項あり。§05 の方針では非推奨 |

**商用利用方針（§05）に従い、既定は Apache-2.0 / MIT のモデルに限定する。**

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

`recursion_limit` は A1 内で 500 固定。ローカル LLM だとループに嵌まると数時間走るため、
**ワーカー側でステップ数の上限とラン全体のウォールクロック上限を別途設ける**（既定 60 ステップ / 30 分）。

## 4.9 将来の差し替え

`ChatOllama` を組み立てている箇所は `agent_factory.py` の 1 関数に閉じる。
vLLM / SGLang（OpenAI 互換）に移す場合は `source="Custom"` + `base_url` で置き換えられる。
クラウド LLM を選択肢に加える場合も同じ関数の分岐で済む設計にしておく。
