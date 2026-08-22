# 21. Web アプリ経由のランで踏んだ 4 つの不具合

実運用のトレースから見つけたもの。どれも**ノートブック経由では起きず、
Web アプリ経由でだけ起きる**か、Claude を選んだときだけ起きる。

## 21.1 構造化入力が丸ごと無駄になっていた（最重要）

実測のプロンプト:

```
Research question:
{'text': 'STAT1阻害薬によって治療される可能性のある…', 'mode': 'hypothesis',
 'organism': 'ヒト', 'context': '', 'focus': ['STAT1'], 'background': '', …}
```

`text` に **dict の repr がそのまま**入っています。しかも `- Organism:` などの
節は空のまま埋め込まれます。§10 で「曖昧な一文では探索の初手が決まらないから
構造化する」と設計したものが、**全部むだになっていました**。

原因は 1 行:

```python
# backend/app/main.py
proc, mp_queue = spawn(run_id, question.as_spec(), settings.model_dump())
#                               ^^^^^^^^^^^^^^^^^ dict

# biomni_hypo/question.py
def coerce_question(value):
    if isinstance(value, ResearchQuestion):
        return value
    return ResearchQuestion.from_text(str(value))   # dict → str(dict)
```

プロセス境界を跨ぐので dict で渡すのは正しい（pydantic モデルは
そのままでは送れない）。**受け側が dict を想定していなかった**のが誤りです。

ノートブックは `ResearchQuestion` をそのまま渡すので素通りし、
パイプラインのテストは文字列を渡していたので、どちらも気付けませんでした。

対処: `coerce_question()` が dict を受け付ける。
往復して同じプロンプトになることをテストで固定します。

## 21.2 Claude の DB ツールが全部 400 で落ちる

```
{'success': False, 'error': "... 400 ... '`temperature` is deprecated for this model.'"}
```

`biomni/tool/database.py::_query_llm_for_api()` は A1 のコンストラクタ引数を見ず、
自分で `get_llm(model=..., temperature=0.0, config=default_config)` を呼びます。
biomni の Anthropic 分岐は temperature を素通しします（`biomni/llm.py:166`）。

```python
return ChatAnthropic(model=model, temperature=temperature, ...)
```

Claude 4.6 以降（Opus 5 / Sonnet 5 / Opus 4.8 …）は `temperature` を受け付けず
400 を返すので、`query_opentarget` などが軒並み落ちます。

**`agent.llm` を差し替えるだけでは直りません。** §4.3 とまったく同じ構造の問題
（default_config を見る経路が別にある）で、強制ポイントが 1 つ増えたということです。

対処: `patch_biomni_get_llm()` で `biomni.llm.get_llm` を包み、
ポリシーの `no_temperature_prefixes` に当たるモデルからは temperature を落とす。
`biomni.tool.database` が `from biomni.llm import get_llm` で握っている名前も
差し替えます（import 済みだと元の関数を持っているため）。

古い Claude 3.5 や Ollama には影響しません。

## 21.3 Ollama はホストのものだけを使う

コンテナ版の Ollama を併用できる構成にしていましたが、

- ポート 11434 がホストの Ollama と衝突する
- 9GB のモデルを 2 つ持つことになる
- `OLLAMA_BASE_URL` がどちらを指しているか分かりにくい（§17 の原因の 1 つ）

割に合いません。**ホストで動いている Ollama だけを使う**方針に統一しました。

`scripts/set-provider.sh` は `ollama` / `both` のとき常に:

- `COMPOSE_PROFILES=""`（ollama コンテナを起動しない）
- `OLLAMA_BASE_URL` を Docker なら `http://host.docker.internal:11434`、
  そうでなければ `http://localhost:11434`
- ホストの Ollama に到達できるか実際に叩いて確かめる
- **127.0.0.1 しか待ち受けていなければ警告する**。コンテナからは届かないため
  （§17.4。`ss` / `lsof` で判定し、どちらも無ければ黙る）

## 21.4 履歴はタブではなく独立ページに

履歴をタブの 5 枚目に置いていましたが、左に入力欄・右に結果で画面が埋まるので
**「そこにある」と気付けません**。

- `/history` を独立ページにする
- ヘッダーに相互リンクを置く
- **検索条件を URL に載せる**（`?q=...&provider=...`）。リロードしても、
  URL を共有しても、同じ絞り込みが開く
- 結果の表示は本体ページに任せる（`/?run=<id>`）。
  計画・論点・根拠・トレースの描画を 2 か所に持つと、必ず片方が古くなる

履歴ページは外部リソースを読みません（自己完結）。テストで固定しています。


## 21.5 `$VAR（…）` が古い bash で壊れる

```
scripts/docker-preflight.sh: line 85: OLLAMA_URL?: unbound variable
make: *** [docker-rebuild] Error 1
```

書いていたのは、変数を確かに代入したあとの行でした。

```bash
OLLAMA_URL=$(env_of OLLAMA_BASE_URL)      # 21 行目で代入している
...
ok "ホストの Ollama を使う設定です（$OLLAMA_URL）"   # 85 行目で unbound
```

原因は `$OLLAMA_URL` の直後が**全角の閉じ括弧**だったこと。
bash 5 は正しく切りますが、**macOS 標準の bash は 3.2**（2007 年）で、
識別子の切れ目を取り違えます。`set -u` の下なので即エラーになります。

日本語でメッセージを出すスクリプトでは踏みやすく、しかも
**書いた環境（bash 5）では再現しません**。`${VAR}` と書けば起きません。

`tests/test_scripts.py` で機械的に禁じます（`$VAR` の直後が非 ASCII なら失敗）。
同時に全スクリプトの `bash -n`、実行権限、`--help` が落ちないことも見ます。
この `--help` の検査で `set-provider.sh --help` が
「`$1` をモードとして食べて exit 1」になっていたのも見つかりました。

## 21.6 起動前チェックの精度

`OLLAMA_PORT=11435`（コンテナ版の衝突回避で入れた値）が残っていると、
ホストの Ollama は 11434 にいるので届きません。起動前チェックで、

- `OLLAMA_BASE_URL` がホストを指しているか（コンテナの `localhost` は自分自身）
- その URL のポートで実際に Ollama が応答するか。しなければ **11434 を試して**
  「11434 では応答しています」と具体的に出す
- `127.0.0.1` だけを待ち受けていないか（§17.4）

を見ます。重さは使い方で変えます。`provider=ollama` なら起動しても仕事に
ならないので**失敗**、Claude 主体なら「Ollama のモデルは選べません」という**警告**に
留めます。Claude だけ使いたい人を止めないためです。


## 21.7 到達性は「推測」ではなく「実測」する

起動前チェックは、待ち受けアドレス（`ss` の出力が `127.0.0.1` か `0.0.0.0` か）から
「コンテナから届かない」と判定していました。**これは推測で、外します。**

- Docker Desktop（macOS / Windows）は `host.docker.internal` から
  ホストのループバックへ転送できることがある
- Linux の `host-gateway` は docker0 のアドレスに解決されるので届かない

環境ごとの正解を覚えるより、**その場で試すほうが確実で短い**です。

```bash
docker compose run --rm --no-deps --entrypoint sh app \
  -c "curl -sf -m 5 '${url}/api/tags'"
```

アプリのイメージには curl が入っており、`extra_hosts` も compose の定義から
効くので、**本番と同じ経路**で試せます。

結果の扱い:

| 実測 | 扱い |
|---|---|
| 届いた | OK |
| 届かない | `provider=ollama` なら失敗、そうでなければ警告 |
| 試せない（イメージ未ビルド） | 待ち受けアドレスから**警告**にとどめる。止めない |

**証明できたときだけ止める。推測では警告に留める。**
初回ビルド前は実測できないので、そこで止めると何も始められません。

## 21.8 `OLLAMA_HOST=0.0.0.0` はポートを壊す

対処として `OLLAMA_HOST=0.0.0.0` と案内していましたが、これは**間違い**です。
`OLLAMA_HOST` はホストとポートの両方を決めるので、ポートを省くと
**既定の 11434 に戻ります**。11435 で動かしている人の設定を壊します。

```
Environment="OLLAMA_HOST=0.0.0.0:11435"     ← ポートを必ず付ける
```

## 21.9 `.env` の `OLLAMA_PORT` を信用しない

コンテナ版との衝突を避けるために `OLLAMA_PORT=11435` へ変えた値が残り、
ホストの Ollama は 11434 にいる、という食い違いが実際に起きました。

`set-provider.sh` は `.env` の値を信用せず、**設定値 → 11434 → 11435 の順に
実際に叩いて**、応答したポートを採用して `.env` に書き戻します。
設定ファイルより、動いているプロセスのほうが真実です。

## 21.10 「接続済み」なのに選べない

Ollama に到達できていても、**商用利用ポリシーで全部弾かれて 1 つも選べない**
状態があります。`llama3.x` / `gemma` しか入っていない環境がまさにそれです。

```
✕ llama3.1:8b    Llama Community License
✕ gemma3:12b     Gemma Terms of Use
✓ qwen3:14b      Apache-2.0
✓ phi4:14b       MIT
✓ deepseek-r1:14b  MIT
✓ mistral:7b     Apache-2.0
```

弾くこと自体は §5 の設計どおりで正しい。問題は**画面の伝え方**でした。

- ヘッダーは「Ollama 接続済み」とだけ出す。使えるモデルが 0 件でも同じ表示
- 選択欄は空のまま。理由も次の一手も出ない

直したこと:

- ヘッダーに**使える数**を出す。`Ollama 接続済み（使えるモデル 3/5）`、
  0 件なら `Ollama 接続済みだが使えるモデルなし`
- 1 つも選べないときは、弾かれたモデルとライセンス名を列挙し、
  `ollama pull qwen3:14b` まで出す

### 埋め込みモデルを一覧に出さない

`nomic-embed-text` などは `ollama list` に並びますが、チャットには使えません。
しかも **Apache-2.0 なのでポリシーでは通ってしまいます**。ライセンスではなく
用途で弾く必要があります（`is_embedding_model()`）。選ばせると、
実行してから壊れます。

## 21.11 動いているビルドを画面に出す

「直したはずなのに変わらない」の大半は再ビルドしていないだけです。
`/api/health` に `build`（git の短縮コミット、Docker 内ではファイルの更新時刻）
を出し、ヘッダーに表示します。

```
商用モード · policy v1 · Ollama 接続済み（使えるモデル 3/5） · v0.1.0 548a2b6
```

これで「いま動いているのは何か」を聞かずに確かめられます。

## 21.12 temperature の対処を「一覧」に頼らない

§21.2 の修正は、ポリシーの `no_temperature_prefixes` に載っているモデルだけ
temperature を落としていました。**これは新しいモデルが出るたびに古くなり、
載っていないモデルで 400 に戻ります。**

この経路（`database.py` の構造化抽出）は `temperature=0.0` を決め打ちで渡して
きますが、送らなければ既定値が使われるだけです。

| | 失うもの |
|---|---|
| temperature を送らない | 決定性が少し落ちる |
| 400 になる | **DB ツールが全滅する** |

比べるまでもないので、**Anthropic なら一律で落とす**ことにしました。
Ollama からは落としません（`temperature=0.7` はそのまま届く）。
未知のモデル名（`claude-future-99`）でも落ちることをテストで固定しています。

### パッチが届いているかをテストで固定する

`database.py` は `from biomni.llm import get_llm` を**モジュール先頭**で行い、
A1 の構築時点で既に import 済みになります。`biomni.llm` を差し替えるだけでは
`database.py` が握っている古い参照は直りません。両方を見ます。

```python
assert database.get_llm is biomni_llm.get_llm, "古い参照が残っている"
```

`<execute>` のコードは `run_python_repl()` が **同一プロセスの `exec()`** で
実行するので（`biomni/tool/support_tools.py`）、プロセス内のパッチが効きます。
別プロセスなら効かないため、ここは前提として押さえておく必要があります。

## 21.13 モデル一覧の取得で画面を止めない

`/api/show` は 1 モデルずつ往復します。逐次に回すと
**モデル数 × 応答時間**のあいだ、モデル選択欄が空のままになります。
実 Ollama はモデル読み込み中に遅くなるので、これは実際に起きます。

8 並列にし、`(base_url, model)` でキャッシュしました
（プロセスの生存中は変わらない値です）。実測: 1 回目 17ms / 2 回目 2ms。

## 21.14 切り分けはアプリ自身に聞く

`make model-check`（`scripts/diagnose-models.sh`）を追加しました。
動いているアプリの `/api/health` と `/api/models` を叩いて、
**画面に出ているものと同じ情報**を表にします。

```
  version=0.1.0  build=e491aee
  選べる: 4 件 / 一覧: 16 件   default='qwen3:14b'
  ✓ qwen3:14b        ローカル  選択可          Apache-2.0
  ✕ llama3.1:latest  ローカル  ポリシーで不可   MAU 条項と命名条項があるため既定で不可
  → 既定 'qwen3:14b' は選択可。画面で選べないなら、
     ブラウザが古い HTML を掴んでいます（強制リロード）。
     ヘッダーの build が git の HEAD と違うなら再ビルドしてください。
```

推測を往復させず、**その環境の事実**から始められるようにするためのものです。

## 21.15 「モデルが全部『未取得』」＝ 別の Ollama を見ている

画面のモデル選択欄が、こうなっていました。

```
ローカル (Ollama)
  deepseek-r1:14b · — 未取得
  gpt-oss:20b · — 未取得
  ★ · qwen3:14b · — 未取得        ← 手元には入っているのに
  qwen3:32b · — 未取得
  ★ · qwen3:8b · — 未取得
```

手元の `ollama list` には 7 件あり、うち `qwen3:14b` `qwen3:8b` `qwen2.5:14b`
`deepseek-r1:8b` はポリシーを通るはずでした。

並んでいるのは**ポリシーの推奨リストそのもの**で、実際に入っている
`llama3.1:latest` `gemma3:4b-it-qat` `cniongolo/biomistral` は**一覧に無い**。
つまり `/api/tags` が**空**を返していた、ということです。

```
reachable      : True   ← 到達はできる
installed 件数 : 0
```

**到達できることと、モデルがあることは別**です。ヘッダーは「接続済み」と出す
ので、この状態が読み取れませんでした。

原因は、アプリが**手元とは別の Ollama** を見ていること。典型は:

- 空の `ollama` コンテナが残っている（`OLLAMA_PORT=11435` はそれを避けるために
  入れた値。裏を返すと、コンテナ版を起動した履歴があるということ）
- `OLLAMA_BASE_URL` が別ポートを指している
- 別ユーザーの `ollama serve`（モデルは `~/.ollama` にあるので別々になる）

### 直したこと

**表示を分ける。** モデル 0 件は「ポリシーで全部弾かれた」とは原因も対処も
まったく違うので、混ぜません。

```
商用モード · policy v1 · Ollama にモデルが 1 件もありません · v0.1.0 fe94d64

  http://127.0.0.1:11435 には Ollama がいますが、モデルが 1 件もありません。
  手元で ollama list にモデルが見えているなら、アプリは別の Ollama を見ています。
  切り分け:
    make model-check
    docker ps | grep ollama   — 残っていれば make docker-down
    bash scripts/set-provider.sh ollama
```

**ポート探索の基準を変える。** これまでは「応答するか」で選んでいたので、
**空のコンテナ版が先に応答すると、そちらを掴んで**いました。
「モデルを何件持っているか」で選びます。

```
$ bash scripts/set-provider.sh ollama       # .env は 11435 を指していた
  ✓ ホストの Ollama を :11434 で見つけました（モデル 5 件）
  ✓ OLLAMA_BASE_URL=http://localhost:11434
```

11435 の空 Ollama を飛ばして 11434 を選びます。
併せて、`ollama` という名前のコンテナが動いていれば警告し、消し方を出します。

## 21.16 ollama コンテナを compose から外す

§21.15 の原因を作ったのは、**profiles で無効化しても消えないコンテナ**でした。

```yaml
ollama:
  container_name: biomni-ollama
  profiles: ["ollama"]
  restart: unless-stopped
```

`COMPOSE_PROFILES` を空にすると、compose はこのサービスを**無視するだけ**で、
既に動いているコンテナは止めません。しかも `restart: unless-stopped` なので
**再起動しても戻ってきます**。実際に 10 時間動き続けていました。

```
786677e963b9  ollama/ollama:latest  "/bin/ollama serve"  Up 10 hours (healthy)
              127.0.0.1:11435->11434/tcp   biomni-ollama
```

`.env` はホストを指しているつもりでも、`OLLAMA_PORT=11435` がこの container を
指していたので、**空の Ollama を見て「モデルが全部 未取得」**になっていました。

ガードを足すより、**原因を消す**ほうが確実です。
「ホストで動いている Ollama だけを使う」と決めた以上（§21.3）、
compose に ollama サービスを持つ理由がありません。

- `ollama` / `ollama-pull` サービスを削除
- `app` の `depends_on: ollama` を削除
- `ollama-models` ボリュームを削除
- `OLLAMA_BASE_URL` の既定を `http://host.docker.internal:11434` に
- `scripts/use-host-ollama.sh` を削除（`set-provider.sh ollama` に統合）
- `COMPOSE_PROFILES` は互換のため `.env.example` に残すが、常に空

既に動いてしまっているコンテナのために、掃除の口を用意します。

```
make docker-stop-ollama     # コンテナだけ消す。モデルはボリュームに残る
```

起動前チェックは、`biomni-ollama` が動いていたら**失敗**します。
残っている限り同じ問題が再発するので、警告では足りません。

### 設計としての教訓

「設定で無効にできる」と「無効にすれば消える」は違います。
`profiles` は**起動しない**だけで、**止める**わけではない。
`restart: unless-stopped` と組み合わさると、
**誰も起動していないのに動き続けるコンテナ**が残ります。
選択肢を残したことが、そのまま罠になっていました。


## 21.17 パッチ対象を名前で並べない

§21.12 のパッチは、差し替えるモジュールを手で並べていました。

```python
for name in ("biomni.tool.database", "biomni.tool.support_tools"):
```

`support_tools` はそもそも `get_llm` を使っておらず、逆に **使っているのに
書いていないモジュールが 4 つ**ありました。

```
$ grep -rl "^from biomni.llm import get_llm" biomni/
  agent/a1.py  agent/react.py  agent/qa_llm.py
  agent/function_generator.py  tool/database.py  tool/genomics.py
```

`from biomni.llm import get_llm` は**自分の名前空間に元の関数オブジェクトを
束縛**します。`biomni.llm` を差し替えても、そちらは古いままです。

名前を並べる限り、biomni の更新で増えたときに漏れます。
**「元の関数を握っているモジュール」を実際に探して**差し替えます。

```python
for module in list(sys.modules.values()):
    if getattr(module, "get_llm", None) is original:
        module.get_llm = get_llm
```

テストも「database.py が直っているか」ではなく、
**「古い参照を握ったままのモジュールが 1 つも無いこと」**を見ます。
