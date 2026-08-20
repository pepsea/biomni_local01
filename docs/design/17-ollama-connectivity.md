# 17. 「Ollama は起動しているのにアプリからは未接続」

## 17.1 症状

ホストで `ollama serve` が動いていて `curl localhost:11434/api/tags` も通るのに、
アプリの `/api/health` が

```json
"ollama": { "reachable": false, "error": "ConnectionError: ... Connection refused" }
```

を返す。

## 17.2 原因 1: compose の `environment:` が `.env` を打ち消していた（バグ）

`docker-compose.yml` の app サービスは `OLLAMA_BASE_URL` をベタ書きしていた。

```yaml
environment:
  OLLAMA_BASE_URL: http://ollama:11434     # ← これが .env より強い
env_file:
  - path: .env
    required: false
```

Compose では **`environment:` が `env_file:` より優先される**。実測:

```
# .env: FOO=from_env_file / compose: FOO: from_environment_block
$ docker compose config
    environment:
      FOO: from_environment_block
```

つまり `scripts/use-host-ollama.sh` が `.env` に
`OLLAMA_BASE_URL=http://host.docker.internal:11434` を書いても、
コンテナの中では `http://ollama:11434` のままだった。
`COMPOSE_PROFILES=""` にして ollama コンテナを起動していないので、
その名前は解決すらしない → `Connection refused`。

**スクリプトは正しく動いていたのに、compose が黙って上書きしていた。**

修正:

```yaml
OLLAMA_BASE_URL: "${OLLAMA_BASE_URL:-http://ollama:11434}"
```

既定は ollama コンテナのまま、`.env` にあればそちらを使う。

## 17.3 原因 2: コンテナの `localhost` はホストではない

`OLLAMA_BASE_URL=http://localhost:11434` のまま Docker で動かすと、
コンテナ自身の 11434 を見に行く。そこには誰もいない。

ホストを指すには `host.docker.internal`（compose の `extra_hosts` で
Linux でも解決できるようにしてある）。`scripts/use-host-ollama.sh` がこれを書く。

## 17.4 原因 3: Ollama が 127.0.0.1 しか待ち受けていない

macOS / Windows の Ollama.app は既定で `127.0.0.1:11434` にだけ束縛される。
`host.docker.internal` はホストのループバックではなくゲートウェイ側の IP に解決されるので、
**ホストからは繋がるのにコンテナからは繋がらない**という状態になる。

```bash
# macOS
launchctl setenv OLLAMA_HOST "0.0.0.0"
# そのあと Ollama.app を終了して起動し直す

# Linux
sudo systemctl edit ollama      # Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
```

## 17.5 原因 4: `.env` を変えたがコンテナが古いまま

`.env` はプロセス起動時に一度だけ読まれる。書き換えたら
`make docker-rebuild`（直接起動なら再起動）が要る。

## 17.6 切り分けの手順

原因が 4 つあり、どれも「Ollama は動いている」ので区別がつかない。
`scripts/diagnose-ollama.sh`（`make ollama-check`）が 3 か所を順に見る。

| 見る場所 | 分かること |
|---|---|
| 1. ホスト → Ollama | Ollama 自体が動いているか。別ポートで待っていないか |
| 2. `.env` の `OLLAMA_BASE_URL` | アプリがどこを見に行くか |
| 3. コンテナ → Ollama | Docker の場合。`host.docker.internal` / `ollama` / 設定値の 3 経路を実際に叩く |

3 で **実際に届いた URL と設定値が食い違っていれば、そのまま直し方を出す**。
どこにも届かなければ §17.4（`OLLAMA_HOST=0.0.0.0`）を案内する。

判定は推測せず、必ず `curl` を打って確かめる。
「動いているはず」で切り分けを進めると、4 つの原因のどれにも辿り着けない。
