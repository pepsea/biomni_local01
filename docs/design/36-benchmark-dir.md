# 36. 見覚えのない `benchmark` が出る

## 症状

```
data/biomni_data/benchmark がないのに探している
```

## 何が起きているか

`A1.__init__` は、データ置き場の中に 2 つのディレクトリを作ります。

```python
benchmark_dir = os.path.join(path, "biomni_data", "benchmark")
data_lake_dir = os.path.join(path, "biomni_data", "data_lake")
os.makedirs(benchmark_dir, exist_ok=True)     # ← 最初に呼ばれるのはこちら
os.makedirs(data_lake_dir, exist_ok=True)
```

**ベンチマークを探しているわけではありません。**
書けない場所だと、最初の `makedirs` が失敗し、その引数である
`benchmark` のパスだけがエラーに出ます。利用者にとっては
まったく見覚えのない名前です。

ベンチマークの**ダウンロード**は
`if expected_data_lake_files is None:` の中にあり、こちらは
必ずリストを渡している（§4.4）ので走りません。実際に確かめました。

```
Skipping datalake download (load_datalake=False)
```

## 直し方

A1 を作る前に、こちらでディレクトリを作って書けることまで確かめます。
駄目ならこちらの言葉で言います。

```
biomni のデータ置き場（data_lake）を作れません: /mnt/…/data/biomni_data/data_lake
  PermissionError: [Errno 13] Permission denied
  実在する一番近い親 : /mnt/…/biomni_local01/data
  そこに書けるか     : False
  ディレクトリの所有者: uid=1000 gid=1000
  いま動いている権限  : uid=501 gid=20
  → 所有者が違います。権限ではなく UID の食い違いです。
```

## 権限の直し方は 2 通り

**ホストで直接動かしている場合**（`start.sh` / systemd）

```bash
sudo chown -R "$(id -u):$(id -g)" data
```

**コンテナで動かしている場合**

bind マウントの所有者はホスト側のままです。コンテナのユーザー
（`APP_UID`、既定 1000）と食い違うと書けません。
**ホストで `chmod` しても、UID が違えば直りません。**

```bash
echo "APP_UID=$(id -u)" >> .env
echo "APP_GID=$(id -g)" >> .env
make update          # ビルド引数なので作り直しが要る
```

診断は `/.dockerenv` の有無で場合を見分け、当てはまるほうだけを出します。

## 診断が診断中に落ちた

`existing_ancestor()` は `Path.exists()` で親を辿りますが、
**途中の親に実行権が無いと `PermissionError` を投げます**。
権限の問題を調べる関数が、権限の問題で落ちていました。

一般ユーザーで実際に走らせて踏みました。`exists()` を握り、
`describe_unusable()` 全体も例外を出さないようにしています。
理由を出すための関数が落ちると、**元の理由まで失われます**。

## 共通化

同じ調べ方を 3 か所で使っていたので `biomni_hypo/paths.py` に寄せました
（ラン保存 §27、データレイク §33、biomni のデータ置き場）。
