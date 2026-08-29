# 38. ツールが名前空間に入っていなかった

## 症状

```
1  execute      # Query PubMed for papers linking IHH/SHH to osteoporosis
2  observation  Error: name 'query_pubmed' is not defined
...
9  execute      # Correctly query PubMed with module path
10 observation  Error: name 'biomni' is not defined
...
14 observation  Error: module 'biomni.tool.database' has no attribute 'query_pubmed'
...
30 observation  Error: name 'query_arxiv' is not defined
```

28 ステップ以上を、**import の当てずっぽう**に費やして終わりました。
これが「回答が得られませんでした」の中身です。

## 原因

biomni の `run_python_repl` は、こう書かれています。

```python
_persistent_namespace = {}
...
exec(command, _persistent_namespace)
```

**空の dict です。ツールは 1 つも入っていません。** モデルは自分で
import しなければなりません。ところがシステムプロンプトのツール一覧には
**どのモジュールにあるかが書かれていません**。

しかも biomni のモジュール分けは推測しにくいものです。

| ツール | 実際のモジュール | モデルが試したところ |
|---|---|---|
| `query_pubmed` | `biomni.tool.literature` | `biomni.tool.database` |
| `query_arxiv` | `biomni.tool.literature` | （名前だけで呼ぶ） |
| `query_uniprot` | `biomni.tool.database` | （名前だけで呼ぶ） |

`query_uniprot` は名前で呼べば正しいのに、名前空間に無いので
`not defined` になります。モデルから見ると「一覧に載っているのに無い」。
延々と当てずっぽうを続けるのは当然でした。

## 直し方

### 1. 案内したツールを名前空間に入れる

`agent.configure()` の直後に、`module2api` に残っているツールを
すべて `_persistent_namespace` に入れます。

```
事前読み込み: 47 個
print(callable(query_uniprot))  -> True
print(callable(query_reactome)) -> True
print(callable(query_pubmed))   -> True
```

**案内したものは呼べる。呼べないものは案内しない。** これで揃います。
どのモジュールにあるかを推測する必要そのものが無くなります。

### 2. それでも無いものを呼んだら、諦めさせる

依存が足りずに落としたツール（`query_scholar` など）は、名前空間にも
入りません。モデルが記憶で呼んだ場合に備えて、観測に
`name 'x' is not defined` が出たら言い直させます。

```
[tool] `query_pubmed` is not available in this environment.
       Do not import it and do not retry it.
       Use one of the available tools instead.
       All available tools are already loaded by name -
       call them directly, without any import statement.
```

「無い」だけでなく **「import は要らない」** まで言うこと。
これを書かないと、次は import で試し始めます。

## 教訓

**一覧に載せたものは、その場で使えなければなりません。**
「載っているが使えない」は、モデルにとって最も高くつく状態です。
人間なら諦めますが、モデルは延々と別の呼び方を試します。

---

## 付記: 読み込んでも import は書かれる

事前読み込みを入れた後も、こう出ました。

```
Error: cannot import name 'query_pubmed' from 'biomni.tool.database'
```

**名前空間に入れても、モデルが import 文を書けば失敗します。**
`query_pubmed` は `literature` にあるので、`database` からは import できません。
呼べる状態にすることと、呼び方を変えさせることは別の仕事でした。

3 つ足しました。

### 1. エラーの形をもう 1 つ拾う

`cannot import name 'X' from 'Y'` は、`name 'X' is not defined` とも
`module has no attribute` とも違う形です。拾っていませんでした。

### 2. 「無い」と「あるのに import した」を言い分ける

助言が正反対になるので、**実際に読み込んだ集合**（`PRELOADED_TOOLS`）を
見て決めます。推測で書き分けると必ずどちらかを間違えます。

```
読み込み済み → [tool] `query_pubmed` is ALREADY loaded. Do not import it.
                Call it directly: `result = query_pubmed(...)` then `print(result)`.
未読み込み   → [tool] `query_pubmed` is not available in this environment. …
```

### 3. プロンプトの規則にする

観測が出てから直させるのは後手です。最初から書かせないようにします。

```
- 一覧にあるツールは既に読み込み済みで、名前だけで呼べる。
  ツールを import してはいけない（`from biomni.tool... import ...` も
  `import biomni...` も書かないこと）。`result = query_uniprot(...)` と直接呼ぶ。
  名前が未定義なら、その環境には無い。別のツールに切り替えること。
```

英語側の規則にも同じものを入れています。

## 教訓（追記）

**「呼べるようにする」と「呼び方を変えさせる」は別の仕事です。**
環境を直しただけでは、モデルは前と同じコードを書きます。
環境・観測への助言・プロンプトの規則、3 つとも要ります。
