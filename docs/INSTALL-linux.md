# 別の Linux マシンに導入する手順

このリポジトリを、まっさらな Linux に入れて常設するまでの手順。
上から順に実行すれば動きます。所要時間はおおよそ **20〜40 分**
（うち大半はイメージのビルドとモデルの取得）。

- 想定 OS: Ubuntu 22.04 / 24.04、Debian 12、RHEL 9 系
- 導入方法は 2 通り。**A（Docker）を推奨**します
  - **A. Docker + systemd で常設** — 再起動しても自動復帰。ホストを汚さない
  - **B. Python 仮想環境で直接起動** — 開発・検証向け

---

## 0. 事前に決めること

| 決めること | 選択肢 | 既定 |
|---|---|---|
| LLM をどこで動かすか | Ollama（ローカル完結） / Claude API / **両方** | 両方が便利 |
| 待ち受けポート | 任意 | 5002 |
| 外部に公開するか | する / しない | しない（後述） |

**Ollama を使う場合の目安**: `qwen3:14b` で VRAM 12GB 程度、または
RAM 32GB（CPU 実行。かなり遅い）。GPU が無ければ Claude API のほうが現実的です。

**ディスク**: イメージ 3GB + モデル 9GB + データレイク数 GB。**30GB 以上**空けてください。

---

## A. Docker + systemd で常設する（推奨）

### A-1. Docker を入れる

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker            # または一度ログアウトして入り直す

docker compose version   # v2 が出ればOK
```

`docker compose version` が失敗する場合は compose v2 プラグインが要ります
（`sudo apt install docker-compose-plugin`）。

### A-2. 取得する

```bash
sudo apt-get install -y git          # 入っていなければ
git clone https://github.com/pepsea/biomni_local01.git
cd biomni_local01
```

### A-3. LLM プロバイダを決める

```bash
# Ollama と Claude の両方を選べるようにする（推奨）
bash scripts/set-provider.sh both --key sk-ant-... --port 5002

# Ollama だけ
bash scripts/set-provider.sh ollama --model qwen3:14b

# Claude API だけ（GPU 不要・イメージも軽い）
bash scripts/set-provider.sh claude --key sk-ant-... --port 5002
```

`.env` が無ければ `.env.example` から作られます。ここで書かれるのは**既定**で、
実際に使うモデルは起動後に画面のプルダウンから実行ごとに選べます。

> `--key` を使わず `.env` に直接 `ANTHROPIC_API_KEY=sk-ant-...` と書いても構いません。
> `.env` は `.gitignore` 済みなので commit されません。

### A-4. 常設する

```bash
make service-install
```

やっていること:

1. Docker / compose / デーモンの確認
2. `.env` の `APP_UID` / `APP_GID` をホストの UID/GID に合わせる
   （bind マウントで `./data` と `./workspace` に書けなくなるのを防ぐ）
3. `data/` `workspace/` の作成
4. systemd ユニットの設置と有効化

**初回は 20〜40 分かかります**（イメージのビルド + モデル 9GB の取得）。
進行は別端末で `make service-logs` を見てください。

sudo を使いたくない場合は `bash scripts/install-service.sh --user`
（ユーザーサービスとして入ります。ただしログイン中しか動きません。
常時動かすには `sudo loginctl enable-linger "$USER"` が別途必要です）。

### A-5. 確認する

```bash
make service-status
curl -s http://localhost:5002/api/health | python3 -m json.tool
```

`"api": "ok"` と、`models.selectable` に使えるモデルが並んでいれば成功です。
ブラウザで `http://localhost:5002` を開いてください。

### A-6. 運用コマンド

| コマンド | 用途 |
|---|---|
| `make service-status` | 状態 |
| `make service-logs` | ログを追う |
| `make service-update` | `git pull` して入れ替え |
| `make service-uninstall` | 取り外す（`data/` `workspace/` は残る） |
| `make update` | `git pull` して、動いている形に合わせて再起動 |
| `make ollama-check` | Ollama に繋がらないときの切り分け |

`.env` を書き換えたら **`make update` が要ります**。
`.env` はプロセス起動時に一度だけ読まれます。

---

## B. Python 仮想環境で直接起動する

Docker を使わない場合。開発・検証向けです。

### B-1. Python 3.11 以上

```bash
python3 --version          # 3.11 以上であること

# Ubuntu 22.04 は既定が 3.10 なので追加する
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv
```

### B-2. セットアップ

```bash
git clone https://github.com/pepsea/biomni_local01.git
cd biomni_local01

bash scripts/setup_local.sh          # 最小構成（テストが通るところまで）
bash scripts/setup_local.sh --full   # biomni + モデル + データセットまで
```

`.venv/` に仮想環境を作り、依存を入れます。
うまく入らないときは `bash scripts/doctor.sh` で
「どの Python にインストールされたか」を確認できます。

### B-3. Ollama を入れる（ローカル実行する場合）

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull qwen3:14b

curl -s http://localhost:11434/api/tags | head -c 200   # 確認
```

### B-4. 起動

```bash
bash scripts/set-provider.sh both --key sk-ant-...   # 省略可
bash scripts/start.sh                                 # http://localhost:5002
bash scripts/start.sh --port 9000                     # ポートを変える
bash scripts/start.sh --check                         # 起動せず確認だけ
```

`start.sh` は起動前に Python 環境・依存・使えるモデル・ポートの空きを確認し、
足りないものを具体的に指摘します。

---

## 公開範囲とセキュリティ

**このアプリは LLM が生成した任意の Python コードを実行します。**
インターネットに直接晒さないでください。

既定では `APP_BIND=0.0.0.0` なので、**同じ LAN からは見えます**。
1 台で閉じたいなら `.env` に:

```
APP_BIND=127.0.0.1
```

を書いて `make docker-rebuild`。他マシンから使いたい場合は
SSH ポートフォワードを推奨します。

```bash
ssh -L 5002:localhost:5002 user@server    # 手元のブラウザで http://localhost:5002
```

どうしても直接公開する場合は、リバースプロキシ（nginx / Caddy）で
Basic 認証か OIDC を前段に置き、ファイアウォールで送信元を絞ってください。
アプリ自体に認証機構はありません。

Ollama コンテナは既定でループバックのみ（`OLLAMA_BIND=127.0.0.1`）に束縛されます。

---

## GPU を使う（任意）

NVIDIA GPU がある場合、Ollama コンテナから使えるようにできます。

```bash
# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   # 確認
```

そのうえで `docker-compose.override.yml` を作ります
（本体を書き換えず、GPU の無いマシンと共存させるため）:

```yaml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

```bash
make docker-rebuild
docker exec biomni-ollama nvidia-smi     # コンテナから GPU が見えるか
```

---

## 動作確認

```bash
# 1. API が生きているか
curl -s http://localhost:5002/api/health | python3 -m json.tool

# 2. 選べるモデル
curl -s http://localhost:5002/api/models | python3 -c \
  "import json,sys; [print(('クラウド' if not m['local'] else 'ローカル'), m['name']) \
   for m in json.load(sys.stdin)['models'] if m['installed'] and m['allowed']]"

# 3. テストを通す（B の構成の場合）
.venv/bin/python -m pytest -q
```

ブラウザで開き、テンプレートから質問を 1 つ実行して、
「回答 / 仮説 / 集めた情報 / 実行トレース / 履歴」の 5 タブが埋まれば導入完了です。

---

## つまずいたとき

| 症状 | 対処 |
|---|---|
| `address already in use` | `lsof -nP -iTCP:5002 -sTCP:LISTEN` で犯人を特定。`start.sh` は起動前に止めて教えます |
| Ollama は動いているのに「未接続」 | **`make ollama-check`**。原因は [17](design/17-ollama-connectivity.md) に 4 つ |
| `permission denied` で `data/` に書けない | `make service-install` が `APP_UID`/`APP_GID` を合わせます。手で入れた場合は `.env` に `APP_UID=$(id -u)` `APP_GID=$(id -g)` |
| `pip install` したのに `ModuleNotFoundError` | `bash scripts/doctor.sh`。別の Python に入っています |
| `No module named 'Bio'` などツールの import 失敗 | `pip install -r requirements.txt`。使えないツールは自動で無効化されます（[04](design/04-ollama-integration.md)） |
| "there are no tags in the current response" | モデルがタグを出せていません。[16](design/16-parsing-errors.md) |
| `docker: permission denied` | `sudo usermod -aG docker "$USER"` して再ログイン |
| モデルの取得が終わらない | 9GB あります。`make service-logs` で進行を確認 |
| 商用ポリシーでモデルが弾かれる | `make models` で理由が出ます。Llama / Gemma 系は既定で不可（[05](design/05-commercial-licensing.md)） |

---

## アンインストール

```bash
make service-uninstall      # systemd ユニットとコンテナを削除（データは残る）

# データも消す場合
docker compose down -v      # ollama のモデルボリュームごと
rm -rf data workspace
```

---

## 関連する設計書

| ドキュメント | 内容 |
|---|---|
| [12-docker](design/12-docker.md) | Docker 構成の意図 |
| [13-linux-deployment](design/13-linux-deployment.md) | systemd 常設の詳細 |
| [15-provider-switching](design/15-provider-switching.md) | Ollama と Claude の切り替え |
| [17-ollama-connectivity](design/17-ollama-connectivity.md) | Ollama に繋がらないときの切り分け |
| [05-commercial-licensing](design/05-commercial-licensing.md) | 商用利用限定の設計 |
