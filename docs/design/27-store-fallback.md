# 27. 保存先が開けないときに逃がす

## 症状

ブラウザで「調べる」を押すと `✗ Internal Server Error` だけが出る。
ページは開き、Ollama は接続済み、モデルも選べている。テストは全件通る。

## 原因

保存先が開けていませんでした。

このリポジトリはネットワークマウント（`/mnt/...`）の下に置かれていました。
sqlite はロックの効かないファイルシステムでは開けず、
`unable to open database file` としか言いません。保存先の既定値は
リポジトリ直下の `workspace/` なので、リポジトリの置き場所がそのまま
保存先の置き場所になっていました。

同じ機械では、以前に import 時点で同じ例外が出ています
（`sqlite3.OperationalError` at `main.py:99`）。あのとき保存先を遅延して
開くようにしたので、**壊れる場所が import 時からラン開始時へ移っただけ**
でした。`POST /api/runs` の `store().save(run)` が最初のアクセスになり、
そこで 500 になります。

- ページは開く（保存先を触らない）
- モデル一覧も出る（触らない）
- 「調べる」で 500（ここで初めて触る）
- pytest は通る（`tmp_path`、つまりローカルディスクを使う）

症状の出方が全部これで説明できます。

## 直し方

理由を出すだけでは足りません。**そこで諦めるとアプリが使えない**ので、
開けなければローカルディスクに逃がします。

```
1. 既定の保存先（HYPO_WORKSPACE、既定はリポジトリ直下）で開く
2. 駄目なら $XDG_STATE_HOME/biomni-hypo/workspace（既定 ~/.local/state/...）
3. そこも駄目なら、両方の理由を並べて 503
```

黙って逃げると「保存したはずの履歴が無い」になるので、
どこに・なぜ逃がしたかを `/api/health` の `store` に出し、画面にも出します。

```
⚠️ 保存先を /home/…/.local/state/biomni-hypo/workspace/runs.sqlite3 に変更しました。
   /mnt/…/workspace/runs.sqlite3 を開けません（…）。
   この場所で残したい場合は .env の HYPO_WORKSPACE を書ける場所に設定してください。
```

`/api/health` 自身は保存先が壊れていても落ちないようにしています
（落ちると「何も分からない」に戻るため）。

## 確認

開けない保存先を指してサーバを起動し、実際に叩いて確かめました。

```
$ HYPO_WORKSPACE=/proc/nope uvicorn backend.app.main:app --port 8124
$ curl -X POST .../api/runs -d '{"question": "...", "model": "qwen3:14b"}'
HTTP 202
```

500 が 202 になりました。実ブラウザでも警告が出ることを確認しています。

## 教訓

**保存先の既定値をリポジトリの中に置くと、リポジトリの置き場所の制約を
そのまま引き継ぎます。** 動く場所に逃がす道を用意しておくこと。

---

## 常駐させる場合は設置時に決める

逃がす仕組みは「動き続ける」ためのものですが、常駐させると
**毎回同じ警告が出続ける**ことになります。警告が常態になると読まれません。

`scripts/install-local-service.sh` が、設置の時点で保存先を決めるようにしました。

1. `.env` の `HYPO_WORKSPACE`（既定はリポジトリ直下）で実際に sqlite を
   開いて**書いてみる**。開くだけでは足りません。NFS は開けても COMMIT で
   落ちます（`probe_workspace()`）。
2. 駄目なら `$XDG_STATE_HOME/biomni-hypo/workspace` を同じやり方で試す。
3. 決まった値を unit / plist に焼き込む。

```ini
[Service]
WorkingDirectory=/mnt/…/biomni_local01
Environment=HYPO_WORKSPACE=/home/…/.local/state/biomni-hypo/workspace
ExecStart=…/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 5002
```

設置時の出力:

```
  ! 保存先 /mnt/…/workspace は使えません
      保存先のディレクトリを作れません: …
  ✓ 保存先: /home/…/.local/state/biomni-hypo/workspace（ローカルディスクに寄せました）
```

この unit で起動すると `/api/health` の `store.fallback` は `null` になり、
画面の警告も出ません。逃げ道は残したまま、常態としては出ないようにします。

`_fallback_workspace()` は `HOME` が無くても決まるようにしました。
systemd のシステムユニット（`sudo systemctl`）では `HOME` が無いことがあり、
`Path.home()` はそこで `RuntimeError` を投げます。

---

## 保存先でアプリを止めない

常駐させたところ、設置は通ったのに起動確認が失敗しました。

```
== 起動確認
  ✗ 応答がありません
      … データベースを開けません …
```

逃げ先は 1 段しか用意していませんでした。既定の場所と
`$XDG_STATE_HOME` の両方が駄目なら `StoreUnavailable` を投げて終わりで、
そうなるとラン開始も履歴も全部落ちます。

**保存先はアプリ全体を止める理由にしてはいけません。** 開ける場所が
見つかるまで順に試し、最後は一時領域まで落ちるようにしました。

```
1. HYPO_WORKSPACE（既定はリポジトリ直下）
2. $XDG_STATE_HOME/biomni-hypo/workspace
3. $TMPDIR/biomni-hypo-<uid>/workspace   ← 最後の逃げ先
```

一時領域は再起動で消えるので、それを画面に明示します。

```
⚠️ 保存先を /tmp/biomni-hypo-0/workspace/runs.sqlite3 に変更しました。
   /proc/nowhere/runs.sqlite3 を開けません（…）。
   ※ 一時領域です。再起動すると履歴は消えます。
```

どこにも保存できない場合だけ 503 にし、**試したすべての場所の理由**を並べます。

### 起動確認の失敗も名指しする

ログをそのまま貼るだけでは、どの行が原因なのか分かりません。
見覚えのある壊れ方（ポート衝突・依存不足・保存先）は名指しして、
直し方とサービスの状態まで出すようにしました。

### 通知が上書きされていた

`renderHints()` は `#hints` の中身を**置き換え**ます。`boot()` は
保存先・Ollama 接続先・モデルの 3 系統から通知を出すので、置き換えで
呼ぶと後のものが前のものを消します。実際、保存先の警告はモデルの警告に
上書きされて画面に出ていませんでした（実ブラウザで確認して気付いた）。

`appendHints()` を足し、`boot()` の中では置き換えを使わないことを
テストで縛りました。
