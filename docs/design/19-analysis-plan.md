# 19. 解析の設計（biomni が最初に立てる計画）

## 19.1 biomni は計画を立てるか → 立てる

`A1._generate_system_prompt()` の冒頭にはっきり書いてあります
（`biomni/agent/a1.py:1058` 付近）。

> **Given a task, make a plan first.** The plan should be a numbered list of steps
> that you will take to solve the task. Be specific and detailed.
> Format your plan as a checklist with empty checkboxes like this:
> ```
> 1. [ ] First step
> 2. [ ] Second step
> ```
> Follow the plan step by step. After completing each step, update the checklist
> by replacing the empty checkbox with a checkmark: `1. [✓] First step (completed)`
> If a step fails or needs modification, mark it with an X and explain why:
> `2. [✗] Second step (failed because...)`
> **Always show the updated plan after each step so the user can track progress.**

つまり biomni の設計では、

1. 最初に番号付きチェックリストで計画を出す
2. **毎ターン計画を再掲**し、済んだ項目に ✓、失敗した項目に ✗ と理由を付ける

`<solution>` が採点用の短答である（§18）のとは対照的に、**計画のほうは
最初から人が追うことを目的に設計されています**（"so the user can track progress"）。

## 19.2 ところが biomni 自身がそれを弾く

同じシステムプロンプトの下のほうに、こう書いてあります。

> In each response, you must include EITHER `<execute>` or `<solution>` tag.
> Not both at the same time. **Do not respond with messages without any tags.**

そして `generate` ノードは、開始タグの無い応答を差し戻します（§16）。

**「まず計画を立てろ」と言いながら、計画だけを返すと弾かれる。**
計画は必ず `<execute>` と同じターンに載せなければなりません。

最初にいただいたエラーログ

```
[ 0] think 1. [ ] Query PubMed …
[ 2] observation Error: name 'query_pubmed' is not defined
… parsing error…
```

の 0 番目は、まさにこの**計画だけを返して弾かれたターン**でした。
§16 の出力形式の再掲に
「Never write a plan without an `<execute>` block」を入れてあるのは、この矛盾への対処です。

## 19.3 このアプリでの扱い（変更前）

計画は出ていました。ただし `_classify()` はタグの無い文章をすべて
`StepKind.THINK` に落とすので、**実行トレースの中に「think」として埋もれていました。**

- 見出しが付かないので、それが計画だと分からない
- 毎ターン再掲されるため、同じ内容が think として何度も並ぶ
- どこまで進んだか、何が失敗したかが、生テキストを読まないと分からない
- 最終回答の画面には出てこない

biomni がわざわざ "so the user can track progress" と書いているものを、
**追跡できない形にしていた**ことになります。

## 19.4 変更後

`StepKind.PLAN` と `PlanItem` を追加し、計画を独立させます。

```python
class PlanItem(BaseModel):
    text: str
    state: Literal["todo", "done", "failed"] = "todo"
    note: str = ""     # "(failed because ...)" の部分
```

### 印の揺れを吸収する

システムプロンプトの例は `[ ]` / `[✓]` / `[✗]` ですが、モデルは平気で
`[x]` `[X]` `[✔]` `[☑]` `[×]` `[-]` を使います。判定を例どおりに厳密化すると、
**モデルを替えた瞬間に計画が拾えなくなる**ので広く受けます。

### 再掲と立て直しを区別する

毎ターン再掲されるので、素直にステップ化すると同じものが何度も並びます。

| 変化 | 扱い |
|---|---|
| まったく同じ | ステップにしない（ノイズ） |
| チェックが進んだだけ | 進捗。PLAN ステップを 1 つ出す。`plan_revisions` は増やさない |
| 手順そのものが変わった | 立て直し。`plan_revisions` を増やす |

`plan_revisions` は「途中で方針を変えた回数」なので、
**結果を読むときの重要な文脈**になります（何度も立て直しているなら探索が迷っている）。

### 最終ターンの計画を取りこぼさない

`<solution>` を含むターンにも計画は載ります。`_classify()` の solution 分岐で
前置きを見ていなかったため、**最後に何が終わって何が失敗したかが残りませんでした**。
`_emit_preamble()` に共通化して、execute / solution の両方で拾います。

### 計画と地の文を分ける

前置きには計画と普通の文章が混ざります。計画の行だけ抜き、
残りを THINK として残します。混ぜたままだと、どちらも読みにくくなります。

## 19.5 表示

- **回答タブ**: 「解析の設計」として回答の直後に出す。☐ / ✓ / ✗、失敗理由、
  進捗バー、`2/4 完了 · 1 件失敗 · 計画を 1 回立て直しました`
- **実行トレース**: 実行中は計画を上部に固定し、更新のたびに書き換える
  （biomni の "track progress" をそのまま実現する）
- **Markdown レポート**: 回答より前に「## 解析の設計」を置く
- **履歴検索**: 計画の各行を `search_text` に入れる。
  「何をやろうとしたか」で過去のランを引ける

計画が 1 つも取れなかった場合は `extra["plan_missing"]` を立てます。
biomni が指示しているものが出てこない = 指示追従性が低い状態なので、§16 と同じ扱いです。

## 19.6 やらないこと

**計画をこちらから与えて biomni に実行させる**ことはしません。
計画はエージェントが探索の中で立て直すものであり（`plan_revisions` が示すとおり
実際に立て直します）、外から固定すると、**行き止まりに当たっても計画どおり進む**
という悪いほうに倒れます。ここでやるのは、立てた計画を**見えるようにする**ことだけです。
