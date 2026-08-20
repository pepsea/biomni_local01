# 15. Ollama と Claude を両方選べるようにする

「今日は手元の qwen3 で回す、この質問だけ Claude で回す」を **.env を書き換えずに**
できるようにする。

## 15.1 何が問題だったか

`.env` には揃えるべき変数が 4 つある。

| 変数 | 誰が見るか |
|---|---|
| `HYPO_PROVIDER` | このアプリ（`Settings`） |
| `HYPO_MODEL` | このアプリ |
| `BIOMNI_SOURCE` / `LLM_SOURCE` / `BIOMNI_LLM` | biomni の `default_config`（§4.3） |
| `COMPOSE_PROFILES` | Docker（ollama コンテナを起動するか） |

`scripts/set-provider.sh` は片方に倒すことしかできず、Claude モードでは
`COMPOSE_PROFILES=""` にして ollama コンテナを止めていた。つまり
**排他**で、切り替えるたびにコンテナの再構築が要った。

## 15.2 決めたこと

**`.env` が決めるのは「既定」だけ。実行ごとのプロバイダはモデル名が決める。**

`apply_model_selection()` は選ばれたモデルの `provider` を `settings.provider` に
書き戻す。ランは毎回 spawn した子プロセスで走り、その中で
`apply_biomni_env(settings)` を **biomni の import より前に**呼ぶので、
ランごとに `BIOMNI_SOURCE` を張り替えられる。

```
ユーザーがモデルを選ぶ
   └─ apply_model_selection ─> settings.provider / settings.model
         └─ worker（spawn した子プロセス）
               └─ apply_biomni_env ─> BIOMNI_SOURCE / LLM_SOURCE / BIOMNI_LLM
                     └─ import biomni      ← ここで default_config が確定する
```

重要なのは、**`ANTHROPIC_API_KEY` が環境にあっても、Ollama を選んだランでは
`BIOMNI_SOURCE=Ollama` が立つ**こと。これがないと `biomni/tool/database.py` の
DB クエリツールだけが黙って Anthropic を呼び、意図しない課金が起きる。
`test_ollama_run_does_not_route_biomni_to_anthropic` で固定している。

## 15.3 `set-provider.sh both`

```
bash scripts/set-provider.sh both --key sk-ant-... --port 8003
bash scripts/set-provider.sh both --default claude --claude-model claude-sonnet-5
```

- `COMPOSE_PROFILES=ollama` のまま（既定が Claude でも ollama コンテナは残す）
- `ANTHROPIC_API_KEY` を設定する
- `--default {ollama|claude}` で `HYPO_PROVIDER` / `HYPO_MODEL` / `BIOMNI_SOURCE` を揃える
- `HYPO_OFFLINE_MODE=false`（オフラインはクラウドと併用できない。§15.5）

従来の `claude` / `ollama` モードは排他のまま残してある。片方しか使わないなら
そちらのほうがリソースを食わない。

`.env` の書き換えは `sed` をやめて Python に寄せた。値に `#` や `/` が入ると
`sed` の区切り文字とぶつかって静かに壊れるため。重複行は最後の 1 行に潰す。

## 15.4 既定の選び方（プロバイダを跨がせない）

クラウドのモデルは `size_bytes = 0` なので、`ModelCatalog.default()` が
サイズ順に選ぶと**必ずローカルが勝つ**。`HYPO_PROVIDER=anthropic` にしても
Ollama に落ちてしまう。

`default(preferred="", provider="")` に `provider` を足し、そのプロバイダに
選択可能なモデルがあれば先に絞る。無ければ従来どおり全体から選ぶ
（Ollama が落ちているときにクラウドへ逃がすため）。

| 既定 | 選んだモデル | 実行に使うプロバイダ |
|---|---|---|
| ollama | qwen3:14b | ollama |
| ollama | claude-sonnet-5 | anthropic |
| anthropic | qwen3:14b | ollama |
| anthropic | 存在しないモデル | anthropic（Claude にフォールバック） |
| ollama | 存在しないモデル（Ollama 到達不可） | anthropic |

## 15.5 オフラインモードとの関係

`HYPO_OFFLINE_MODE=true` は「質問文を一切外部に出さない」という約束なので、
クラウドのモデルとは併用できない。`apply_model_selection()` が
`ModelNotAvailable` を投げる。

実行してからエラーになると分かりにくいので、UI 側でもクラウドのモデルを選んだ
時点でオフラインのチェックを外して無効化し、ローカルに戻したら元に戻す
（`updateModelNote()`）。判定の正は常にサーバ側に置き、UI は先回りするだけ。

## 15.6 選択肢の見せ方

モデル一覧は「ローカル (Ollama)」と「クラウド (Claude API)」の `optgroup` に分ける。
**キーが未設定でも Claude を一覧から隠さない**。`installed=false` と
「`ANTHROPIC_API_KEY` が未設定です」という理由を添えて出す。
何を設定すれば使えるようになるかが分からなくなるほうが害が大きい。

クラウドを選んだときは選択欄の直下に
「⚠️ クラウド実行。質問文と実行結果が Anthropic に送信されます」を出す。
価格（$/1M トークン）も一覧に載せる。
