# 13. Linux に常設する（Docker + systemd）

## 13.1 なぜ systemd まで要るのか

`docker-compose.yml` の `restart: unless-stopped` だけでも、コンテナが落ちれば
Docker が再起動してくれる。ただし「常設」としては足りない。

| 欲しいこと | `restart: unless-stopped` だけ | systemd を足すと |
| --- | --- | --- |
| クラッシュからの復帰 | ✅ | ✅ |
| マシン再起動後の復帰 | △ docker.service が enable されている場合のみ | ✅ 明示的に依存を宣言する |
| 起動順（ネットワーク待ち） | ❌ | ✅ `After=network-online.target` |
| `systemctl start/stop/status` で扱う | ❌ | ✅ |
| ログを journald に集約 | ❌ | ✅ |
| 更新手順を 1 コマンドに | ❌ | ✅ `systemctl reload` |

**systemd は「起動と停止の入口」、コンテナの `restart: unless-stopped` は「自己回復」**
と役割を分ける。

## 13.2 導入

```bash
git clone <このリポジトリ> && cd biomni_local01
make service-install
```

やること:

1. Docker / compose / デーモン / systemd の確認
2. `.env` の用意と **`APP_UID` / `APP_GID` をホストに合わせる**（§13.5）
3. `data` / `workspace` の作成
4. systemd ユニットの設置・有効化・起動

http://localhost:8000 が開く。初回はイメージのビルドとモデル取得（約 9GB）で
数分〜数十分かかる。進行は `docker compose logs -f ollama-pull` で見える。

### sudo を使いたくない場合

```bash
bash scripts/install-service.sh --user
```

ユーザーユニット（`~/.config/systemd/user/`）として入る。
ログアウトしても動かすには linger が要るので、スクリプトが
`loginctl enable-linger` を試みる。失敗したら手動で:

```bash
sudo loginctl enable-linger $USER
```

## 13.3 運用

| したいこと | コマンド |
| --- | --- |
| 状態 | `make service-status` |
| ログ（アプリ） | `make service-logs` = `docker compose logs -f app` |
| ログ（systemd） | `sudo journalctl -u biomni-hypo -f` |
| 再起動 | `sudo systemctl restart biomni-hypo` |
| 停止 | `sudo systemctl stop biomni-hypo` |
| 自動起動を切る | `sudo systemctl disable biomni-hypo` |
| **更新** | `make service-update`（`git pull` + `systemctl reload`） |
| 取り外す | `make service-uninstall` |

`reload` は `docker compose up -d --build` を走らせる。コードを変えたらこれ。

## 13.4 ユニットの中身

`deploy/biomni-hypo.service`。設置時に `__WORKDIR__` / `__USER__` / `__DOCKER__` が
実環境の値へ置換される。

```ini
[Unit]
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=1800          # 初回のビルドとモデル取得
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose up -d --build --remove-orphans
```

**`Restart=` は書かない。** `Type=oneshot` との併用は systemd が拒否する
（"Restart= setting other than no ... isn't allowed for Type=oneshot"）。
自己回復はコンテナ側の `restart: unless-stopped` が担う。

ユーザーユニットとして入れる場合、インストーラが `Requires=docker.service` と
`After=docker.service` を落とす。ユーザーユニットからシステムユニットへ
`Requires=` はできないため。

## 13.5 UID の一致（Linux で最初に詰まる点）

`./data` と `./workspace` をバインドマウントするので、**コンテナ内のユーザーと
ホストのユーザーの UID がずれると書き込めない**。

対策として、イメージのビルド時にホストの UID/GID を渡す。

```
Dockerfile:        ARG APP_UID=1000 / ARG APP_GID=1000
docker-compose.yml: build.args から ${APP_UID} ${APP_GID}
install-service.sh: .env に id -u / id -g を書く
```

自分の UID が 1000 でない場合でも、これで合う。
手で `make docker-up` する場合は、先に `.env` へ書いておくこと。

```bash
echo "APP_UID=$(id -u)" >> .env
echo "APP_GID=$(id -g)" >> .env
```

すでに root 所有で作ってしまった場合:

```bash
sudo chown -R $(id -u):$(id -g) data workspace
```

## 13.6 ディスクとログ

| 置き場 | 中身 | 目安 |
| --- | --- | --- |
| Docker ボリューム `ollama-models` | pull したモデル | 5〜20GB |
| `./data` | Biomni データレイク | 数百 MB〜（許可リスト分だけ） |
| `./workspace` | ラン履歴（sqlite）・レポート・図 | 徐々に増える |
| Docker のログ | | 上限を設定済み（app 10MB×5 / ollama 10MB×3） |

`docker compose down` ではボリュームもバインドも消えない。
完全に消すなら `bash scripts/uninstall-service.sh --purge`。

## 13.7 公開する場合

既定では `8000` をすべてのインタフェースで待ち受ける。**認証は無い**（01 §1.3）。
LAN や外部に出すなら、前段にリバースプロキシを置いて認証を掛けること。

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";      # SSE を切らないため
    proxy_buffering off;                 # ← 実況が届かなくなるので必須
    proxy_read_timeout 3600s;            # ラン中は長時間つなぎっぱなし
    auth_basic "biomni";
    auth_basic_user_file /etc/nginx/.htpasswd;
}
```

`proxy_buffering off` を忘れるとリアルタイム表示（11 §11.1）が動かない。
外に出さないなら `docker-compose.yml` の app のポートを
`127.0.0.1:8000:8000` に変えるだけでよい。

Ollama のポートは最初から `127.0.0.1` にバインドしてある。

## 13.8 検証状況

このリポジトリの CI 環境には Docker デーモンが無いため、次までしか確認していない。

- ✅ `docker compose config` による構文検証
- ✅ `systemd-analyze verify` によるユニット検証
- ✅ インストーラの前提チェック・`.env` 生成・ユニット描画（docker をスタブに差し替えて実行）
- ❌ 実際のビルド・起動・再起動後の復帰

実機で `make service-install` を実行して確認すること。
