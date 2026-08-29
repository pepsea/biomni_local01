# 26. 停止処理が自分自身を撃っていた

## 症状

`bash scripts/setup_local.sh` が、テストの段で 1 行だけ残して死にました。

```
== テスト
Terminated
```

pytest の失敗ではありません。`Terminated` は SIGTERM で殺されたプロセスに対して
シェルが出すメッセージです。テスト結果も、どこで止まったかも残っていません。

## 原因

`backend/app/worker.py` の `terminate_tree()` は、ランを止めるときに
**プロセスグループごと** SIGTERM を送っていました。

```python
os.killpg(os.getpgid(pid), signal.SIGTERM)
```

biomni は `run_bash_script` などでさらに孫プロセスを起こすので、
`proc.terminate()`（直接の子にしか届かない）では足りず、グループを撃つ必要がある —
という判断自体は正しいものでした。

問題は **どのグループを撃つか** です。
子は `run_in_subprocess` の先頭で `os.setsid()` を呼んで自分のグループを作りますが、
それが効くのは spawn した Python が起動し切ってからです。`proc.start()` 直後の
数百 ms〜数秒（依存の import が多く、遅いファイルシステムならもっと長い）、
**子はまだ親と同じプロセスグループに居ます**。

その間に `os.getpgid(pid)` を撃つと、返ってくるのは親自身のグループです。
つまり親が自分を撃ちます。しかも非対話シェルでは、スクリプト本体と
pytest は同じプロセスグループに居るので、**スクリプトごと道連れ**になります。
`pytest -q 2>&1 | tail -3` と書いてあったため、パイプのバッファに溜まっていた
出力も一緒に消え、`Terminated` の 1 行だけが残りました。

同じ壊れ方は Web アプリでも起きます。ランを開始した直後に「停止」を押すと、
API サーバのプロセスグループに SIGTERM が飛び、**サーバごと落ちます**。

## 再現

修正前のコードに対して、新しいテストを 1 本走らせるだけで再現します。

```
$ pytest -q tests/test_cancel.py::test_terminate_tree_never_signals_our_own_process_group
Terminated
```

利用者が見たものと同じ 1 行です。

## 直し方

「子が自分で作ったグループ」だと確認できたときだけ撃ちます（`_own_group()`）。

1. `os.getpgid(pid)` が自分のグループと**違う**なら、それを撃つ（従来どおり孫まで届く）。
2. 同じなら、分かれるまで最大 2 秒待つ。spawn の起動待ちはこれで吸収できる。
3. それでも同じなら、グループは**撃たない**。`proc.terminate()` に落とす。

3 に落ちると孫プロセスが残りえますが、それは「自分を殺す」よりはるかに軽い失敗です。
警告としてログに残します。

回帰テストは、SIGTERM のハンドラを一時的に差し替えて
「自分に届いたか」を数えます。届いていたら失敗させます。
ハンドラを入れずに書くと、テスト自身が死んで結果が残りません。

## 付随して直したこと

`scripts/setup_local.sh` のテスト実行は、出力をパイプに流していたため、
シグナルで殺されると何も残りませんでした。

- `logs/pytest.log` に残す（殺されてもファイルは残る）。
- 終了コードが 128 を超えていたら、シグナルを**名前で**報告する。
  「テストの失敗ではない」と明示し、SIGKILL ならメモリ不足、
  SIGTERM なら誰かがグループごと止めている、と切り分け先を出す。

```
  ✗ pytest が SIGTERM で殺されました（テストの失敗ではありません）
      止まった場所（logs/pytest.log の末尾）:
        ...
```

## 教訓

`os.killpg(os.getpgid(pid), ...)` は、`pid` が自分の子であることを確かめただけでは
安全になりません。**その子が自分のグループを抜けたか**まで確かめる必要があります。
抜ける前の窓は、spawn では常に存在します。

---

## 付記: `Internal Server Error` の 7 文字

同じ日に、画面に `✗ Internal Server Error` とだけ出る報告がありました。
これも「理由が残らない」種類の壊れ方です。

FastAPI の既定の 500 は**本文が空**です。フロントは
`b.detail || r.statusText` と書いていたので、表示できるのは HTTP の
ステータス文字列だけになります。手元で 1 人が使う道具なので、
隠す意味がありません。

- `StoreUnavailable` → 503。`RunStore` がその場で調べた理由をそのまま返す。
- それ以外の未処理例外 → 500。型・メッセージ・どのエンドポイントか・
  traceback の末尾 2000 字を JSON で返し、サーバ側にも `log.exception` で残す。
- フロントは `where` と `traceback` も表示する。`.msg` を `white-space: pre-wrap`
  にして、診断の改行を潰さないようにした。

これで、次に同じことが起きたときは画面に原因が出ます。

---

## 付記 2: テストが開発機の Ollama を掴んでいた

`bash scripts/setup_local.sh` が 1 件だけ失敗する報告がありました。

```
FAILED tests/test_api.py::test_run_without_ollama_is_rejected - assert 202 == 422
1 failed, 364 passed, 17 skipped
```

`test_run_without_ollama_is_rejected` は「Ollama が居なければラン開始前に
422 で止める」ことを見るテストです。ところが `client` fixture は
`ollama_base_url` を指定していませんでした。既定値は
`http://localhost:11434` — つまり **開発機で動いている本物の Ollama** です。

Ollama を立てていない機械（CI やこちらの検証環境）では届かないので 422 になり、
テストは通ります。**Ollama を立てている利用者の環境でだけ 202 になって落ちます。**
このプロジェクトでは Ollama が動いているほうが普通の状態なので、
落ちるほうが多数派です。

既定のポートで模擬 Ollama を立てると、手元でもそのまま再現しました。

```
$ python fake_ollama.py &          # 127.0.0.1:11434 で /api/tags を返す
$ pytest -q tests/test_api.py::test_run_without_ollama_is_rejected
FAILED ... assert 202 == 422
```

`client` fixture が `http://127.0.0.1:1`（誰も待ち受けていないポート）を
向くようにしました。他のテストは元から模擬サーバか同じアドレスを明示して
いたので、穴はこの fixture だけでした。

同じことが起きないよう、fixture の向き先そのものを見るテストを 1 本置いています。
模擬 Ollama を既定のポートで立てたまま全件走らせて、422 passed を確認しました。

**テストが環境から拾ってよい既定値はありません。** 外に出る設定は
fixture 側で必ず塞ぐこと。
