# 34. ワーカーが落ちたときに理由が消えていた

## 症状

Linux では

```
failed · 0 ステップ · - 秒
```

とだけ出て、回答が得られない。同じコードが mac では動く。

## 原因

理由は最初から届いていました。捨てていたのはこちら側です。

子プロセス（`run_in_subprocess`）は、例外を必ず捕まえて親に返します。

```python
except Exception as exc:
    queue.put({"kind": "error", "payload": {"error": …, "traceback": …}})
```

親（`_drain`）はそれを SSE に流すだけで、**保存していませんでした**。
そして `finally` で `run.error or "ワーカーが結果を返さずに終了しました"` を
入れるため、実際の例外は上書きもされず、ただ消えます。

画面側にも受け口がありませんでした。`token` / `step` / `phase` /
`input_hints` / `done` は拾っていますが、`error` は拾っていません。
`showResult` も `r.error` を表示していません。

結果、**例外の型もメッセージも traceback も、どこにも出ません**。
`failed · 0 ステップ · - 秒` は「例外が起きた」ことだけを意味する記号でした
（`duration_sec` が `-` なのは、`emit("done")` まで到達していないため）。

## 予約名の罠

直そうとして気付きました。SSE のイベント名 `error` は
**EventSource の予約名**です。`event: error` のメッセージは
`es.onerror` に配られます。つまりこのまま `error` で送ると、
ワーカーの例外が届くたびに接続エラーとして扱われ、
ストリームが閉じます（§29 の再接続処理も誤作動する）。

サーバ側の名前を `run_error` に変えました。

## 直し方

1. `_drain` が `error` を受けたら、`run.error` と
   `extra["error_traceback"]`（末尾 4000 字）を保存する。
   保存しないと、履歴から開き直したときに何も残らない。
2. SSE には `run_error` として流す（予約名を避ける）。
3. 画面はラン中も結果表示でも `showRunError()` で出す。
   型・メッセージ・traceback をそのまま、折り返して表示する。
4. 「ワーカーが結果を返さずに終了しました」には
   「メモリ不足で OS に強制終了された場合もここに来ます」を足した。
   子が SIGKILL されると例外すら送れないので、この文言だけが残る。

## 確認

例外で終わったランを保存し、実ブラウザで開きました。

```
failed · 0 ステップ · - 秒
❌ ModuleNotFoundError: No module named 'sklearn'

Traceback (most recent call last):
  File "biomni_hypo/agent_factory.py", line 188, in build_agent
    agent = A1(...)
ModuleNotFoundError: No module named 'sklearn'
```

## 教訓

**環境差（Linux では落ちるが mac では動く）は、理由が出ていれば大半が一目です。**
理由を捨てていると、環境の違いを総当たりで探すことになります。
情報は届いていたのに、受け口が無くて捨てていた ── §30、§31 と同じ形です。
