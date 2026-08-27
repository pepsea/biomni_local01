# 25. 小さいモデルは早く満足する

## 25.1 症状

同じ問いで、

- Claude: 6 手
- qwen3:14b: **3 手**

手数が半分です。しかも打ち切られたのではなく、**モデルが自分で
`<solution>` を書いて終えています**。

## 25.2 これは「能力」ではなく「止め時の判断」の差

小さいモデルが 3 手で終えるのは、3 手ぶんしか調べられないからではありません。
**3 手で「もう分かった」と判断する**からです。satisficing（そこそこで満足する）
と呼ばれる挙動で、指示追従性とは別の軸です。

仮説構築では、これが直接ダメージになります。

| | Claude 6 手 | qwen3 3 手 |
|---|---|---|
| 当たった情報源 | 複数 | 1〜2 |
| 反証を探す余地 | ある | ほぼ無い |
| §3 の根拠モデル | 厚く積める | 1 クエリ頼み |

**1 回のクエリしか支えが無い仮説は、そのクエリがどれだけ綺麗でも弱い。**

## 25.3 対処: 進捗を数字で見せて押し戻す

§22 で入れた「毎ターン最後尾に念押しを置く」仕組みを**適応型**にします。
まだ浅ければ、形式の念押しに加えて深さの押し戻しを足します。

```
[depth] You have run only 2 of at least 4 data queries.
Do not write <solution> yet. Query another INDEPENDENT source
(a different database or a different aspect) with <execute>.
```

要点は **具体的な数を見せる**ことです。「よく調べよ」では効きません。
`2 / 4` と出すと、モデルは残りを埋めにいきます。

数えるのは **モデル自身が出した `<execute>`** だけです。observation に
`<execute>` の文字列が出ても数えません（水増しを防ぐ）。

閾値に達したら押し戻しは止まります。**掘り続けさせるのが目的ではありません。**

### 実測

同じモデル（押し戻しが無ければ即 `<solution>` を書く挙動）で:

```
押し戻しなし        → execute 0 回 / solution='結論'
押し戻しあり（min=4）→ execute 4 回 / solution='結論'
```

### ブロックはしない

`<solution>` を**拒否**する実装にはしていません。押し戻すだけです。
モデルが「もう本当に無い」と判断すれば結論を書けます。
拒否すると、材料が尽きたときに終われなくなります（§23 で見たとおり、
止まらないループのほうが害が大きい）。

## 25.4 プロンプト側にも書く

`_EN_RULES` / `_JA_RULES` に足しました。

```
- Consult SEVERAL INDEPENDENT sources before concluding. One database is not enough:
  a hypothesis supported by only one query is weak, however clean that query looked.
- Do not stop at the first plausible answer. Check whether it also holds in a
  second, different kind of data.
```

こちらは会話の先頭側なので §22 のとおり手数が進むと薄れます。
効くのは押し戻しのほうで、プロンプトは初手の方向づけです。

## 25.5 設定

```
HYPO_MIN_EXPLORATION_STEPS=4     # 0 で無効
```

4 の根拠: 独立した情報源を 3〜4 当たれば、支持と反証の両方が拾える見込みが立つ。
それ以上を強制すると、材料が無い問いで空振りを繰り返します。

**context との兼ね合いに注意**（§22）。1 手あたり約 3,300 トークンなので、
`num_ctx=40,960` / システムプロンプト 18.6k なら回せるのは約 6 手です。
`HYPO_MIN_EXPLORATION_STEPS` をそれに近づけると、探索の途中で context が
尽きます。**深さを増やしたいなら、先に context を増やす**必要があります。

## 25.6 手数だけ増やしても意味が無い場合

DB ツールが軒並み失敗していると（§24）、押し戻しても空振りが増えるだけです。
その場合は先に §24（`HYPO_TOOL_QUERY_MODEL`）を見てください。
**引けないまま手数だけ増やすと、エージェントは自分の記憶で埋め始めます**（§20.1）。
