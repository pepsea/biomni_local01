# 41. 助言そのものが誤りを教えていた

## 症状

```
1 observation  Error: unhashable type: 'slice'
5 observation  Error: unhashable type: 'slice'
7 observation  Error: unhashable type: 'slice'
3 observation  Error: expected an indented block after 'if' statement
9 observation  Error: expected an indented block after 'if' statement
```

## 原因 1: こちらが書いた例が壊れていた

コードを見て分かりました。

```python
result_uniprot = query_uniprot("FGFR1 function in bone development")
print("UniProt results summary:", result_uniprot[:800])
```

`r[:800]` は**文字列にしか使えません**。辞書に使うと
`unhashable type: 'slice'` です。

そしてこの形を書いたのは、§40 で私が足した助言文でした。

```
"Print a slice (`print(r[:800])`) or search it ..."
```

**モデルは助言どおりに書いて、助言のせいで落ちていました。**
3 回とも同じ原因です。

`str()` を必ず通す形に直しました。

```
"Print `print(str(r)[:800])` - str() first, ALWAYS."
```

助言に出すコード例が、辞書で壊れる形になっていないことを
テストで縛っています（`NUDGE` で終わる定数を全部走査する）。
自分の書いた例は、自分では疑いません。機械に見張らせること。

## 原因 2: ブロックを書き切れない

```python
if isinstance(result_uniprot, dict) and 'results' in result_uniprot:
print("UniProt results count:", len(result_uniprot['results']))
```

`if` の次の行がインデントされていません。2 回とも同じ形です。
小さいモデルは、複数行のブロックを安定して書けません。

直し方は「インデントを直せ」ではありません。**分岐を書かせないこと**です。

```
[syntax] Your code did not parse. Write FLAT code in <execute>:
         no `if`, no `for`, no `try` - just one statement per line.
         `print(str(r)[:800])` works for every result type, so you do not
         need any isinstance check. Re-send the same queries without the branches.
```

ここが繋がります。`print(str(r)[:800])` が**どの型でも通る**なら、
`isinstance` の判定は要りません。判定が要らなければ `if` も要らず、
インデントの問題は起きません。**1 つの安全な形を教えることで、
2 つの失敗が同時に消えます。**

プロンプトの規則も、選択肢を並べるのをやめて 1 つに絞りました。

```
- 結果を丸ごと print しないこと。どの型でも通る形はこれ 1 つ:
  `print(str(r)[:800])`。`print(r[:800])` と書かないこと。
- <execute> の中は平らに書くこと。`if`・`for`・`try` を使わない。1 行 1 文。
  `print(str(r)[:800])` はどの型でも通るので型の判定は要らない。
```

## 教訓

**選択肢を並べると、モデルは壊れるほうを選びます。**
§40 では 3 つの書き方を並べ、そのうち 1 つが辞書で壊れる形でした。
弱いモデルに教えるときは、**必ず通る形を 1 つだけ**示すこと。

そして、こちらの助言も検証の対象です。テストは実装だけでなく、
**実装が出す文言**にも要ります。
