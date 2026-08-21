# 20. 関数の中で import するツール（`No module named 'pymed'`）

## 20.1 症状と、本当の被害

実際のトレース:

```
[ 2] execute     # query PubMed for STAT1 inhibitors …
[ 3] observation Error: No module named 'pymed'
[14] execute     # use arxiv to find recent research …
[15] observation Error: No module named 'arxiv'
…
[34] think       Let me compile the research manually based on
                 what I've found and my knowledge base
[35] execute     import json …
```

`No module named` そのものより、**34 行目が本当の被害**です。
文献を引く手段を全部失ったエージェントが、**自分の記憶で書き始めています。**

このアプリの前提は「仮説構築に使用したデータとその根拠を示す」ことなので、
ここが崩れると成果物の意味が無くなります。検証器（§3）が
「実行したコードの出力に識別子が現れたか」を見るので**捏造した PMID は
`failed_citations` に落ちます**が、それは事後の話で、
そもそもランが無駄になっています。

## 20.2 なぜ既存のガードを素通りしたか

§4 の `_drop_unimportable_modules()` は**モジュール単位**で検査していました。

```python
importlib.import_module("biomni.tool.literature")   # ← 通る
```

ところが biomni のツールは、依存を**関数の中で** import します
（`biomni/tool/literature.py:158`）。

```python
def query_pubmed(query, max_papers=10, max_retries=3):
    from pymed import PubMed          # ← ここ
```

モジュールの import 時には評価されないので、モジュール検査は通ります。
`pymed` は biomni の依存宣言にも requirements.txt にも入っていなかったため、
**「案内されるが呼ぶと落ちるツール」**になっていました。

`No module named 'Bio'`（§4）と同じ失敗の再来ですが、
**遅延 import は同じ手では捕まらない**という点が違います。

## 20.3 対処 1: 足りない依存を入れる

| パッケージ | 使うツール | ライセンス |
|---|---|---|
| `pymed` | `query_pubmed` | MIT（商用可） |
| `arxiv` | `query_arxiv` | MIT（商用可） |

`query_pubmed` はこのアプリで**いちばん重要な文献ツール**です。
これが動かないと PMID が 1 つも取れず、§3 の根拠モデルが空回りします。

## 20.4 対処 2: 呼べないツールは最初から見せない

一般解として `_drop_tools_with_missing_lazy_imports()` を追加しました。
各ツール関数のソースを **AST で読み**、関数内の `import` 文を集め、
`find_spec` で存在を確かめます。**実行はしません**（副作用が無い）。

```python
tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)): ...
```

### `try:` の中の import は無視する

作者が不在を織り込んで代替に落としている場合があります。
これを理由にツールごと外すと、**動くはずのものまで消えます**。
`Try` ノード配下の import は数えません。

### 効果（実測）

| | ツール数 | システムプロンプト | num_ctx=32768 の占有 |
|---|---|---|---|
| 変更前・絞り込みなし | 154,296 字 | — | **超過（38.6k トークン）** |
| 変更後・絞り込みなし | 110 | 74,492 字 | 57% |
| 変更後・CORE プリセット | 47 | 38,129 字 | 29% |

**67 件のツール**が「呼べないのに案内されていた」状態でした
（`scipy` / `sklearn` / `rdkit` / `matplotlib` / `cobra` / `transformers` などを
関数内で import するもの）。外した分そのままプロンプトが軽くなっています。

数値は「どの optional パッケージが入っているか」で動くので、
テストは絶対値ではなく **CORE との比**で見ます。

## 20.5 検証済みバージョンを固定する

`biomni>=0.0.8` を `biomni==0.0.8` にしました。
§4 の落とし穴（`get_llm` が `stop_sequences` を渡さない、`database.py` が
`default_config` を見る、`A1` がデータレイクを一括取得する、システムプロンプトが
巨大）は**すべて 0.0.8 の実装を読んで実測したもの**です。
新しい版が出た瞬間に、ビルドし直しただけで前提が変わるのは危険なので固定します。
上げるときは `tests/test_integration_biomni.py` を通してからにします。

## 20.6 「読み込めません」を自己診断できるようにする

`Dependency.installed` は `find_spec` なので「見つかるか」しか分かりません。
**入っているが import すると落ちる**（壊れたネイティブ拡張、依存の非互換、
途中で失敗したビルド）を OK と答えてしまいます。

```
find_spec の判定: True
実際に import すると: ImportError libfoo.so が無い
```

そこで:

- `probe_import()` — 別プロセスで実際に import し、例外の中身を持ち帰る。
  別プロセスにするのは、biomni の import が重く副作用があり、
  最悪落ちても API 本体を巻き込まないため
- `/api/health` に `biomni: {ok, version, error, python}` を出す
- 起動時に一度検査し、失敗ならログに理由を出す
  （質問を投げて子プロセスが落ちるまで誰も気付かない、を無くす）
- `scripts/diagnose-app.sh`（`make app-check`）— Docker なら**コンテナの中**で、
  そうでなければ手元の Python で、実際に import してみる

推測しないこと。「入っているはず」で進めると、この 4 通りの原因のどれにも辿り着けません。
