# 12. Docker で常駐させる

## 12.1 なぜ Docker を勧めるか

1. **「どの Python か」問題が消える。** イメージの中に Python は 1 つ、
   `/opt/venv` だけ。`pip install したのに ModuleNotFoundError` が起きない
2. **依存が固定される。** biomni の未宣言依存（pandas / tqdm / biopython /
   beautifulsoup4 / PyPDF2 / googlesearch-python、§4.0）を毎回踏まない
3. **隔離される。** LLM が生成した任意コードを実行するので、
   ホストと分けたい（02 §2.6）
4. **常駐する。** `restart: unless-stopped` で、明示的に止めるまで動き続ける

## 12.2 起動

```bash
make docker-up          # docker compose up -d --build
```

http://localhost:8000 が開く。

| コマンド | 用途 |
| --- | --- |
| `make docker-up` | ビルドして常駐起動 |
| `make docker-logs` | ログを追う（初回のモデル取得もここで見る） |
| `make docker-ps` | 状態 |
| `make docker-down` | 停止。**モデルとデータは残る** |
| `make docker-rebuild` | コード変更を反映して app だけ再起動 |

## 12.3 サービス構成

```
ollama        Ollama 本体。restart: unless-stopped。モデルは名前付きボリュームに残る
ollama-pull   モデルを取ってくるだけの使い捨て。取得済みなら即終了する
app           FastAPI。ollama が healthy になってから起動する
```

`docker compose ps` で `ollama-pull` が `Exited (0)` になっているのは正常。
1 回走って終わる設計。

### 初回の挙動

`ollama-pull` がモデル（既定 `qwen3:14b`、約 9GB）を取り終えるまで、
UI のモデル一覧は空のままになる。取得が終わったら画面の
`↻ 再読み込み`（`GET /api/models?refresh=true`）を押す。

進行は `make docker-logs` で見える。

## 12.4 常駐の意味

`restart: unless-stopped` は次のように振る舞う。

| 出来事 | 挙動 |
| --- | --- |
| コンテナがクラッシュ | 自動で再起動 |
| Docker デーモン再起動 / マシン再起動 | 自動で復帰 |
| `docker compose down` / `docker stop` | **復帰しない**（明示的に止めたため） |

つまり「一度上げたら、自分で止めるまで動き続ける」。

## 12.5 永続化されるもの

| パス | 中身 | 消えるか |
| --- | --- | --- |
| `ollama-models`（名前付きボリューム） | pull したモデル | `docker compose down -v` で消える |
| `./data`（バインド） | Biomni データレイク | 残る |
| `./workspace`（バインド） | ラン履歴（sqlite）・レポート | 残る |
| `./config`（読み取り専用） | リソースポリシー | ホスト側が正 |

`down` にボリュームは巻き込まれない。数 GB のモデルを取り直さずに済む。

## 12.6 Claude API を使う場合

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo "HYPO_PROVIDER=anthropic" >> .env
echo "HYPO_MODEL=claude-opus-5" >> .env
make docker-up
```

compose が `.env` を読んで app に渡す。この場合 Ollama は無くても動くが、
モデル取得（`ollama-pull`）は走るので、不要なら
`docker compose up -d ollama app` のようにサービスを絞る。

**質問文と実行結果が Anthropic に送信される。** UI にも警告が出る（11 §11.2）。

## 12.7 セキュリティ設定

LLM が生成したコードを実行するため、app は次のように絞ってある（02 §2.6）。

```yaml
security_opt: [no-new-privileges:true]
cap_drop: [ALL]
mem_limit: 8g
pids_limit: 512
user: app (uid 1000)          # Dockerfile で作成
```

Ollama のポートは `127.0.0.1:11434` にバインドしてあり、外部には出ない。

## 12.8 GPU を使う場合

NVIDIA GPU があるなら `docker-compose.yml` の `ollama` に足す。

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

`nvidia-container-toolkit` がホストに要る。CPU のみでも動くが、
14B のモデルは 1 トークンあたり数百 ms かかるので、まず `qwen3:8b` で試すとよい。

## 12.9 イメージ構成

2 段ビルド。コンパイラを実行イメージに持ち込まない。

```
builder : python:3.11-slim + build-essential -> /opt/venv に requirements.txt を install
runtime : python:3.11-slim + curl（healthcheck 用） + /opt/venv をコピー
```

`HEALTHCHECK` は `/api/health` を叩く。このエンドポイントは
依存の充足状況と使えるモデルまで見ているので、`docker compose ps` の
`healthy` が「実際に使える状態」とほぼ一致する。

## 12.10 ポートが衝突するとき

```
Error response from daemon: ports are not available:
exposing port TCP 127.0.0.1:11434 -> ...: bind: address already in use
```

**ホストに直接入れた Ollama が既に 11434 を使っている**のに、compose が
Ollama コンテナも立てようとしたときに出る。`make docker-check` で起動前に分かる。

```bash
make docker-check          # 誰がそのポートを使っているかまで出る
```

対処は 3 つ。

### (A) ホストの Ollama をそのまま使う（おすすめ）

コンテナを二重に立てない。モデルの再取得も起きない。

```bash
bash scripts/use-host-ollama.sh
make docker-rebuild
```

`COMPOSE_PROFILES` を空にして ollama コンテナを止め、
`OLLAMA_BASE_URL=http://host.docker.internal:11434` を設定する。
Linux でこの名前を解決するために、compose の app に
`extra_hosts: ["host.docker.internal:host-gateway"]` を入れてある。

### (B) ホストの Ollama を止めて、コンテナ版に寄せる

```bash
sudo systemctl stop ollama     # または起動中のプロセスを終了
sudo systemctl disable ollama  # 自動起動も切るなら
make docker-up
```

モデルはコンテナ側のボリュームに取り直しになる（数 GB）。

### (C) コンテナ版を別ポートにする

```bash
echo "OLLAMA_PORT=11435" >> .env
make docker-up
```

ホストの Ollama と併存する。アプリはコンテナ側を使う。

### アプリ側のポートが衝突する場合

同じく `make docker-check` で分かる。`.env` の `APP_PORT` を変える。

```bash
echo "APP_PORT=8003" >> .env && make docker-rebuild
```

## 12.11 検証状況

このリポジトリの CI 環境では Docker デーモンが使えないため、
**`docker compose config` による構文検証までしか行っていない**。
実機で `make docker-up` して確認すること。詰まりやすい点:

- `./data` と `./workspace` の所有者。コンテナは uid 1000 で動く。
  Linux で自分の uid が 1000 でない場合は `sudo chown -R 1000:1000 data workspace`
- 初回ビルドは pandas / biopython などのダウンロードで数分かかる
- モデル取得はさらに時間がかかる（回線次第で数十分）
