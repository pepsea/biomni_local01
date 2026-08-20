# 16. タグ無し応答（"there are no tags in the current response"）

実測で出た症状:

```
0 think  Each response must include thinking process followed by either <execute>
         or <solution> tag. But there are no tags in the current response.
         Please follow the instruction, fix and regenerate the response again.
```

## 16.1 何が起きているか

biomni の `A1` は ReAct ループの各ターンで、モデルの出力から
`<execute>` / `<solution>` / `<think>` を正規表現で探す
（`biomni/agent/a1.py` の `generate` ノード）。

```python
if "<execute>" in msg and "</execute>" not in msg:   # 閉じタグは補ってくれる
    msg += "</execute>"
...
if answer_match:    state["next_step"] = "end"
elif execute_match: state["next_step"] = "execute"
elif think_match:   state["next_step"] = "generate"
else:
    print("parsing error...")
    # 2 回まで差し戻し、3 回目で打ち切る
```

つまり **閉じタグの欠落は救済されるが、開始タグが 1 つも無い応答は救済されない**。
モデルが平文の計画だけを返すと差し戻され、2 回続くと
`Execution terminated due to repeated parsing errors.` でランが終わる。

実際に踏んだ出力はこれ:

```
1. [ ] Query PubMed for BRCA1
2. [ ] Check GWAS Catalog
3. [ ] Summarise
```

指示追従性の低いローカルモデルが「まず計画を書く」挙動に入ったまま、
コードブロックへ進まないパターン。

原因は主に 3 つ:

1. **`num_ctx` が小さく、システムプロンプトが切り詰められている**（§4.5）。
   タグの規定はシステムプロンプトにしか無いので、切られると当然守れない。
   絞り込みなしの A1 システムプロンプトは 38.6k トークンで `num_ctx=32768` を超える
2. **`num_predict` が小さく、`<think>` の途中で生成が尽きる**
3. **モデルの指示追従性が足りない**（小さいモデル、非 instruct 版）

## 16.2 対処

### (a) フレームワークの差し戻しを think として出さない

`biomni_hypo/tracing.py` に `StepKind.PARSING_ERROR` を足し、
差し戻し・打ち切りの定型文を専用の種別に分類する。

- 画面には日本語で**原因と対処**を出す（英文の叱責をそのまま見せない）
- 原文は `Step.error` に残す（あとから追えるように）
- 発生回数を `TraceResult.parsing_errors` に数え、`RunResult.extra` に載せる
- 打ち切りは `stopped_reason` に入れる

差し戻しは `think` と紛らわしいが、**モデルが考えているのではなく
フレームワークが怒っている**。ここを区別しないと、
「エージェントが何か考えているらしい」と誤読して原因に辿り着けない。

なお**差し戻し自体は致命的ではない**。1 回差し戻されたあとタグ付きで返せば
ランはそのまま続く（`test_run_recovers_after_a_retry`）。警告であって失敗ではない。

### (b) 出力形式を毎ターン近くに置く

`biomni_hypo/question.py` の `_FORMAT_REMINDER` を、組み立てるプロンプトの末尾に
必ず付ける。システムプロンプトにも同じ規定はあるが、それは会話の先頭にあり、
手数が増えるほど遠ざかる。ユーザーメッセージ側に置けば毎ターン近くに残る。

```
Output format (required, every single turn):
- Write your reasoning first, then EXACTLY ONE of the following tags.
- To run code:  <execute>...python code...</execute>
- To finish:    <solution>...final answer...</solution>
- A reply containing neither tag is discarded. Never write a plan without an <execute> block.
- Never write <observation> yourself; it is filled in for you.
```

タグはリテラルなので、日本語プロンプトでもこの部分は英語のままにする。
最後の 1 行は別の落とし穴（§4.1、`<observation>` の自己生成）への予防でもある。

### (c) 直らないときに試すこと

| 症状 | 対処 |
|---|---|
| 差し戻しが毎ターン出る | `HYPO_NUM_CTX` を上げる。上限はモデル依存で、超えた分は自動で丸められる |
| `<think>` の途中で切れている | `HYPO_NUM_PREDICT` を上げる（既定 4096） |
| どちらを上げても直らない | モデルを替える（qwen3:14b 以上、または Claude） |
| システムプロンプトの占有率警告が出ている | ツールモジュールのプリセットを `CORE_TOOL_MODULES` に落とす（§4.5） |

## 16.3 やらないこと

**biomni にパッチを当てて、平文の計画を `<execute>` として拾う**ことはしない。
コードのつもりでない文章を実行してしまう。biomni 側は `` ``` `` のコードブロックを
`<execute>` とみなすフォールバックを既に持っており、それ以上の推測は危険。

差し戻しを我々の側で握り潰して再試行することもしない。biomni の 2 回上限は
無限ループを防ぐためにあり、外から回数を増やすと**同じ失敗にトークンを使い続ける**。
上限に当たったら、原因（context か num_predict かモデル）を人に見せて選ばせる。
