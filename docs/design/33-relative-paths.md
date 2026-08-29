# 33. 相対パスの設定値

## 症状

```
bash scripts/setup_local.sh --full
…
== データセット（許可リストのうち最小限）
Traceback …
FileNotFoundError: no such file or directory: data/biomni_data/data_lake
```

パスが**相対**です。これが手掛かりでした。

## 原因

`.env.example` にこう書いてあります。

```
BIOMNI_PATH=./data
```

`Settings.data_path` はこの値をそのまま持ち、`pathlib.Path("./data")` は
**プロセスの作業ディレクトリ**を基準に解決されます。つまり同じ設定が、
実行した場所によって別の場所を指します。

- `bash scripts/setup_local.sh` → スクリプトが `cd` するのでリポジトリ直下
- `jupyter lab notebooks/` から実行 → `notebooks/data/...`
- systemd の `WorkingDirectory` が違う → その場所
- 子プロセスが `chdir` した後 → その場所

既定値（`REPO_ROOT / "data"`）は絶対パスなので、**`.env` を作った人だけが
踏みます**。`.env.example` をコピーした全員が該当します。

## 直し方

パスの設定値は、読み込んだ時点で絶対パスにします。基準はリポジトリの
ルート（`.env` が置いてある場所）です。

```python
def _env_path(name: str, default: str) -> str:
    value = _env(name, default)
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (REPO_ROOT / path).resolve())
```

`BIOMNI_PATH` / `HYPO_WORKSPACE` / `HYPO_POLICY_PATH` の 3 つに適用しました。
絶対パスで書いた設定はそのまま尊重します。

テストでは、作業ディレクトリを変えても同じ場所を指すことを見ています。
これが本題なので、値の一致だけでなく `chdir` してから確かめること。

## 併せて: 失敗の理由を分ける

取得に失敗すると、原因を問わず

```
✗ データセット取得に失敗（ネットワークを確認）
```

と出していました。実際は置き場所の問題だったので、見当違いです。
例外から次に見るところを 1 つに絞るようにしました。

| 例外 | 出す案内 |
|---|---|
| `PermissionError` | 書き込み権限がありません: `<パス>` |
| `FileNotFoundError` | 置き場所が見つかりません。相対パスの説明と絶対パスの例 |
| `OSError(errno=28)` | ディスクの空きがありません。`df -h` |
| timeout / connection / ssl / dns | ネットワークに繋がりません |
| その他 | そのまま報告してください |

置き場所は先に `mkdir` と書き込み可否を確かめ、駄目ならダウンロードに
入る前に止めます。またデータセットが無くてもアプリは動くので、
`setup_local.sh` はここで止まらず、あとで `make fetch` と案内します。

---

## 「作れません」だけでは打つ手が無い

絶対パスに直したうえで、まだ作れない環境がありました。

```
データレイクを作れません: /mnt/…/biomni_local01/data/biomni_data/data_lake
```

理由は 1 つではありません。権限が無い、読み取り専用でマウントされている、
容量が足りない、親ディレクトリから存在しない ── どれも同じ 1 行になります。
§27（保存先）と同じで、**その場で調べて添える**ようにしました。

```
データレイクを作れません: /proc/nowhere/biomni_data/data_lake
  FileNotFoundError: [Errno 2] No such file or directory: '/proc/nowhere'
  実在する一番近い親 : /proc
  そこに書けるか     : True
  空き容量           : 0.0 GB
  → 空きが足りません（最小構成で 0.3 GB ほど要ります）

  書ける場所を 1 つ見つけました: /home/…/biomni-data
  .env にこの 1 行を書いて、もう一度実行してください:
      BIOMNI_PATH=/home/…/biomni-data
```

見るのは 5 つです。

1. 例外の型とメッセージ
2. 実在する一番近い親（どこから先が無いのか）
3. そこに書けるか
4. 空き容量（足りなければ名指しする）
5. 読み取り専用マウントか（`/proc/mounts` を最長一致で引く）、
   ネットワークマウントか

そのうえで、**実際に書けると確かめた場所**を 1 つ提案します。
`$HOME/biomni-data` に試し書きして、成功したものだけを出すこと。
「たぶん書けるはず」の場所を案内すると、往復がもう 1 回増えます。

データレイクは大きいので、保存先（§27）のように黙って移すことはしません。
どこに置くかは利用者が決めるべきで、こちらは決められる材料を出します。
