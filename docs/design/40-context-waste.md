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
