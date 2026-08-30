# 40. 観測が文脈を食い潰す

## 症状

```
0 execute      # Query UniProt for FGFR1 details
1 observation  The output is too long to be added to context.
               Here are the first 10K characters...
```

問い合わせ自体は成功しています。にもかかわらず、この 1 ステップで
**文脈の数千トークンを失い**、得られたのは切れた JSON の頭だけです。

ローカルモデルの文脈は 40,960（qwen3:14b）で、1 ステップあたりの
見積もりは 3,300 トークンでした（§22）。10K 文字は約 2,500〜3,000 トークン、
**1 ステップ分を丸ごと食う**計算になります。数回やれば、古い方から
落ちていき、タグの規則ごと消えます。

## 原因

biomni のシステムプロンプトにはこうあります。

> When calling the existing python functions ... YOU MUST SAVE THE OUTPUT
> and PRINT OUT the result.

正しい指示ですが、**「全部出せ」とは書いていない**のに、モデルは
`print(result)` と書きます。API の応答は数百 KB になることがあります。

## 直し方

2 層で止めます。

### 1. プロンプトの規則（最初から書かせない）

```
- 結果を丸ごと print しないこと。変数に受けて、必要な部分だけ出す
  （`print(list(r.keys()))`、`print(r['results'][0])`、`print(str(r)[:800])`）。
  切り詰められた観測は数千トークンを食う割に、ほとんど何も分からない。
```

### 2. 起きてしまったときの助言

```
[context] That output was truncated - it cost thousands of tokens and gave
          little. Never print a whole API result. Assign it to a variable,
          then print ONLY what you need: `print(type(r), len(r))`,
          `print(list(r.keys())[:20])`, `print(r['results'][0])`,
          or `print(str(r)[:800])`.
          Do not re-run the same query just to see more of it.
```

**「見たさに再実行するな」まで書くこと。** これが無いと、続きを見ようとして
同じクエリを投げ直し、また切り詰められます。

切り詰めの助言は、一般の「API が失敗した」より具体的なので優先します。

## 教訓

**成功した観測も、文脈を壊します。** エラーばかり見ていましたが、
探索が浅くなる原因として、成功した巨大な出力のほうが質が悪い。
エラーは 1 行ですが、成功した JSON は 10,000 文字です。

---

## 返り値は辞書とは限らない

```
0 execute      # Query PubMed for papers linking FGFR1 to osteoporosis
1 observation  Error: string indices must be integers, not 'str'
2 execute      # Check the structure of the PubMed query results
3 observation  <class 'str'>
```

ステップ 2 は**正しい対処**です（前節の助言どおり、丸ごと出さずに
`type()` を見た）。ただ、1 ステップ使って分かったのが「文字列だった」
だけなのは惜しい。

`query_pubmed` は署名にこう書いてあります。

```python
def query_pubmed(...) -> str:
```

**宣言されているのに、モデルは辞書だと思って `r['results']` と書きます。**
biomni のツールは返り値が揃っておらず、文字列・辞書・DataFrame が混在します。

### 直し方

ツールを名前空間に入れるとき（§38）、返り値の**注釈がある**ものだけ
型名も控えます。47 個中 9 個に注釈がありました。注釈が無いものは
黙ります。推測で型を言うと、別の間違いを誘発します。

```
[type] `query_pubmed` returns a plain `str`, not a dict.
       Do not index it with keys. Print a slice (`print(r[:800])`)
       or search it (`if 'FGFR1' in r:`).
       Tool results differ: some are `str`, some are `dict`,
       some are DataFrames - check with `print(type(r))` before indexing.
```

型が分からないツールには「That tool」と一般形で言い、断言しません。

プロンプトの規則にも入れました。

```
- ツールの返り値は辞書とは限らない。文字列のものも DataFrame のものもある。
  添字で取り出す前に `print(type(r))` を見ること。
```

## この一連で分かったこと

観測から学べることは、エラーの文面だけではありません。

| 観測 | こちらが足せる情報 |
|---|---|
| `unexpected keyword argument` | 実物の署名（§37） |
| `name 'x' is not defined` | 読み込み済みか否か（§38） |
| `cannot import name` | import は要らない（§38） |
| `Invalid fields parameter value` | `fields=` を外せ（§39） |
| `success: False` | 同じ問いを引ける別の DB（§39） |
| `output is too long` | 何を print すべきか（§40） |
| `string indices must be integers` | 実物の返り値の型（§40） |

いずれも **こちらが持っている情報で、モデルが持っていないもの** です。
渡さなければ、モデルは総当たりで探し、文脈を使い切ります。
