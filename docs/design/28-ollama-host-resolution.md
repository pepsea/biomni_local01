# 28. ローカルの Ollama を確実に掴む

## 何度も起きたこと

同じ 1 台の Ollama でも、呼ぶ側がコンテナの中か外かで名前が変わります。

| 呼ぶ側 | 届く URL |
|---|---|
| ホストのプロセス（`scripts/start.sh`） | `http://localhost:11434` |
| コンテナの中（`docker compose`） | `http://host.docker.internal:11434` |

`.env` は git 管理外なので、**pull しても直りません**。Docker 用に設定した
`host.docker.internal` が残ったままホストで起動すると、ホストではその名前が
引けないので「Ollama 未接続」になり、モデルが 1 つも選べません。逆も同じです。

この構図で `docs/design/17`、`21 §21.3`、`21 §21.15` と繰り返し踏んでいます。
そのたびに「`bash scripts/set-provider.sh ollama` を実行してください」と
案内してきましたが、設定を配れない以上、また起きます。

## 直し方

**届く先を自分で見つけます。** 設定された URL に届かなければ、
実行形態違いの別名を試します（ポートは保つこと）。

```python
ALTERNATE_OLLAMA_HOSTS = {
    "host.docker.internal": ("localhost", "127.0.0.1"),
    "localhost":            ("host.docker.internal",),
    "127.0.0.1":            ("host.docker.internal",),
}
```

切り替えたら `SETTINGS.ollama_base_url` を直します。子プロセスは
`settings.model_dump()` を受け取るので、ここで直せばワーカーも
エージェントも同じ Ollama を見ます。

黙って切り替えてはいけません。別の Ollama を掴むと `ollama list` と
画面のモデル一覧が食い違い、原因が分からなくなります（§21.15 で実際に
起きた）。`/api/health` の `ollama.fallback` に出し、画面にも出します。

```
⚠️ Ollama の接続先を http://localhost:11434 に切り替えました。
   設定値 http://host.docker.internal:11434 には届きません（.env の OLLAMA_BASE_URL）。
   揃えるには: bash scripts/set-provider.sh ollama
```

届く設定はそのまま使います。余計な切り替えはしません。

## 確認

`.env` が Docker 用のままホストで起動した状態を作り、実ブラウザで見ました。

```
ヘッダ : 商用モード · policy v1 · Ollama 接続済み（使えるモデル 1/1）
選択肢 : ★ · qwen3:14b · 9.3GB · ctx 40,960 / …
選択中 : qwen3:14b
```

以前はここが「Ollama 未接続」でモデルが空でした。

## 教訓

**設定は配れません。** pull で直せないところ（`.env`、ホスト名、置き場所）で
壊れる経路は、案内ではなく、届く先を自分で見つける実装で塞ぐこと。
