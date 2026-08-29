# 35. Ollama がコンテナで動いている場合

## 症状

```
Linux : failed · 0 ステップ · - 秒 ／ LLM で使えるモデルがありません
mac   : 問題なく動く
```

## 原因

**モデルの置き場が違います。**

Ollama をコンテナで動かすと、モデルはコンテナの中（またはそこにマウント
されたボリューム）に置かれます。ホストの `~/.ollama/models` とは別物です。

つまり、

- ホストで `ollama pull qwen3:14b` → ホストの Ollama にだけ入る
- コンテナの Ollama から見ると → **モデルは 0 件**

`ollama list` をホストで打つと入っているように見えるので、
「あるのに使えない」という見え方になります。
mac ではホストの Ollama をそのまま使っていたので動いていました。

アプリ側から見ると、到達はできる（`reachable: true`）が
`/api/tags` が空、という状態です。選べるモデルが 0 件なので、
ランは始まる前に失敗します。

## 直し方

**コンテナの中に pull します。**

```bash
docker ps | grep ollama                             # コンテナ名を確認
docker exec -it <コンテナ名> ollama list             # 中に何があるか
docker exec -it <コンテナ名> ollama pull qwen3:14b   # Apache-2.0・推奨
```

商用ポリシーで使えるのは Apache-2.0 / MIT のものです
（`qwen3:14b`、`deepseek-r1:14b`、`phi4:14b` など）。
`llama3.1` は MAU 条件があるため弾かれます。

## 案内を直した

これまで「コンテナ版が残っていれば `make docker-down`」と案内していました。
**コンテナ版が本番の構成である場合を想定していなかった**ためです。
消させるのではなく、中に入れる方法を出すようにしました。

- `scripts/diagnose-models.sh` は `docker ps` を見て、Ollama のコンテナが
  あればコンテナ名込みの `docker exec … ollama pull` を出します。
- 画面の「モデルが 1 件もありません」も同じ内容にしました。

```
== Ollama のコンテナ
  ollama  ollama/ollama:latest  0.0.0.0:11434->11434/tcp
      モデルはコンテナの中に置かれます。ホストで pull したものは見えません。
…
  → アプリが見ている Ollama には、モデルが 1 件もありません。
     Ollama はコンテナで動いています。モデルはコンテナの中に入れてください。
       docker exec -it ollama ollama list
       docker exec -it ollama ollama pull qwen3:14b     # Apache-2.0・推奨
```

## 注意: アプリもコンテナの場合

アプリ自身もコンテナで動かすなら、`localhost` はコンテナ自身を指すので
Ollama には届きません。両方をコンテナで動かす場合は、同じネットワークに
置いてサービス名で呼ぶか、`host.docker.internal`（Linux では
`extra_hosts: host-gateway`）を使ってください。
§28 の解決処理は `host.docker.internal` ⇄ `localhost` を自動で試します。

## 教訓

**「残っているから消す」と「これが本番だから使う」は、見た目が同じです。**
同じ `docker ps | grep ollama` の出力に対して、対処が正反対になります。
案内を書くときは、利用者の構成を決めつけないこと。
