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
