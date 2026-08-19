# 11. リアルタイム出力とプロバイダ選択

## 11.1 Biomni でリアルタイム出力は可能か → できる

A1 は `self.llm.invoke(messages)` を**同期で**呼ぶので、一見すると
「メッセージが 1 つ出来上がるまで何も見えない」構造に見える。

しかし `ChatOllama._generate` は内部でストリーミングしており、チャンクごとに
LangChain のコールバック `on_llm_new_token` を発火する。**biomni を一切改変せずに
トークン単位の実況が取れる**（`ChatAnthropic` も `streaming=True` で同じ）。

```
A1.generate ─> llm.invoke(messages)      ← 同期。ここは変えない
                    │
                    └─ ChatOllama._generate
                            └─ 内部でストリーム ─> on_llm_new_token ─> TokenStreamHandler
                                                                            │
                                                          on_event("token", …) ─> SSE ─> ブラウザ
```

実測（モック Ollama、実物の A1 グラフ）:

```
[生成開始] |GWAS Catalog| を確認します。
<ex|ecute>
print|('PMID: 1752|9967 rs29815|82 FGFR2')
<|/execute> [完了]
```

### 実装

| 部品 | 役割 |
| --- | --- |
| `llm.TokenStreamHandler` | `on_llm_new_token` を受けて sink へ流す。ランごとに sink を差し替える |
| `agent_factory` | `ChatOllama(callbacks=[handler])` で仕込み、`AgentBundle.token_stream` に保持 |
| `tracing.TracingRunner` | ラン開始で sink を接続し、終了で切る。`on_event("token", …)` を発行 |
| `backend/app/main.py` | SSE で配信。**トークンは永続化しない** |
| `static/index.html` | 「いま生成中」欄にカーソル付きで追記 |

トークンを永続化しない理由: 毎秒数十件流れるうえ、再接続時に読み直しても意味がない
（確定したステップだけ残ればよい）。`_EPHEMERAL_EVENTS` で明示している。

sink はランの外では `None` にする。リソース検索など、ユーザーに見せる必要のない
内部呼び出しまで実況してしまうため。

### 粒度

| レベル | 何が見えるか | 遅延 |
| --- | --- | --- |
| **トークン** | LLM が今書いている文字 | 即時 |
| **ステップ** | 思考 / コード / 実行結果 / ブロック | メッセージ完了時 |
| **フェーズ** | 調査中 → 回答を組み立て中 → 根拠を検証中 | フェーズ切替時 |
| **結果** | 回答・仮説・根拠・使用リソース | ラン完了時 |

ローカル LLM は遅い。**待ち時間を情報に変える**のがこの 4 段構えの狙い（07 §7.6-3）。

## 11.2 プロバイダ: Ollama と Claude API

| | Ollama | Claude API |
| --- | --- | --- |
| データ | ローカルから出ない | **質問文と実行結果が Anthropic に送信される** |
| 必要なもの | `ollama pull <モデル>` | `ANTHROPIC_API_KEY` |
| context | モデル依存（qwen3:14b で 40,960） | 1M（Opus 5 / Sonnet 5 / Opus 4.8） |
| 費用 | 電気代 | $1〜$5 / 1M 入力トークン |
| オフラインモード | 併用可 | **併用不可**（選ぶと弾く） |

### なぜ選択肢に入れるか

§4.5 で測ったとおり、A1 のシステムプロンプトは絞り込んでも 16.5k トークンある。
`num_ctx=32768` のローカルモデルでは会話に使える余白が半分しかない。
**1M context の Claude ならこの制約が消える**ので、ツールモジュールを絞る必要もなくなる。

代わりにローカル完結の前提が崩れる。UI・CLI・レポートで必ず明示する。

### 実装上の注意

- **Claude 4.6 以降は `temperature` を受け付けず 400 を返す。** 既定では送らない。
  `resource_policy.yaml` の `no_temperature_prefixes` で管理し、
  `policy.supports_temperature(provider, model)` で判定する
- biomni の Anthropic 分岐は Ollama と違い `stop_sequences` をきちんと渡す（§4.1 の問題は無い）。
  それでも自前の `build_chat_anthropic()` を通すのは、ストリーミングと温度の扱いを揃えるため
- `apply_biomni_env()` は `BIOMNI_SOURCE` を `Anthropic` に切り替える。
  これを忘れると DB クエリツールが Ollama を向いたままになる（§4.3 の裏返し）
- 抽出フェーズの `format`（JSON Schema 強制）は Ollama 固有。Claude ではプロンプトの
  指示に任せる

### モデル ID

`config/resource_policy.yaml` の `providers.anthropic.models` に列挙する。
記憶で書かず、Anthropic の公式リファレンスから取ること。ID に日付サフィックスは付けない
（`claude-opus-5` であって `claude-opus-5-2026xxxx` ではない）。

## 11.3 検証方法

本物の API キーが無くても、モックサーバで配線を確認できる。
`biomni_hypo/mock_ollama.py` は Ollama の `/api/chat` と Anthropic の `/v1/messages`
（SSE 形式）の両方を実装している。

`tests/test_integration_biomni.py` が固定していること:

- トークンが**ステップより先に**届く（実況になっている）
- `start` / `end` が LLM 呼び出し回数と一致する
- ランを抜けたら sink が外れている
