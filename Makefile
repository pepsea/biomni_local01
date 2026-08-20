.PHONY: help install install-min test lint notebook up api check-env doctor ollama-check models fetch check \
	docker-up docker-down docker-logs docker-ps docker-rebuild docker-check \
	service-install service-status service-logs service-update service-uninstall

help:
	@echo "install      依存を全部入れる（biomni 含む・重い）"
	@echo "install-min  最小構成（テストとノートブック 03 まで動く）"
	@echo "test         pytest"
	@echo "lint         ruff"
	@echo "notebook     JupyterLab を起動"
	@echo "up           Web アプリを起動（環境確認つき）  ← いつもこれ"
	@echo "check-env    起動せず環境だけ確認"
	@echo "doctor       どの Python を使っているか診断（依存が入らないとき）"
	@echo "ollama-check Ollama に繋がらないときの切り分け"
	@echo "api          uvicorn を直接起動（確認なし）"
	@echo "models       ローカルの Ollama にあるモデルを一覧"
	@echo "fetch        許可リストのデータセットを取得"
	@echo "check        lint + test"
	@echo ""
	@echo "-- Docker（常駐させる場合）--"
	@echo "docker-up      ビルドして常駐起動（初回はモデル取得で時間がかかる）"
	@echo "docker-logs    ログを追う"
	@echo "docker-ps      状態を見る"
	@echo "docker-down    停止（モデルとデータは残る）"
	@echo "docker-rebuild コード変更を反映して再起動"
	@echo "docker-check   起動前チェックだけ（ポート衝突など）"
	@echo ""
	@echo "-- Linux に常設（systemd）--"
	@echo "service-install   常設する（Docker + systemd）"
	@echo "service-status    状態"
	@echo "service-logs      ログ"
	@echo "service-update    git pull して入れ替え"
	@echo "service-uninstall 取り外す（データは残る）"

install:
	pip install -r requirements.txt

install-min:
	pip install pydantic pyyaml requests pytest

test:
	pytest -q

lint:
	ruff check biomni_hypo backend tests scripts

notebook:
	jupyter lab notebooks/

up:
	bash scripts/start.sh

check-env:
	bash scripts/start.sh --check

ollama-check:
	bash scripts/diagnose-ollama.sh

doctor:
	bash scripts/doctor.sh

api:
	uvicorn backend.app.main:app --reload --port 8000

models:
	python scripts/list_models.py

fetch:
	python scripts/fetch_datasets.py

check: lint test

# ---- Docker -----------------------------------------------------------------
# restart: unless-stopped なので、明示的に down するまで動き続ける。
# マシンを再起動しても Docker が上がればアプリも戻る。

# ポートは .env の APP_PORT で変える（既定 8000）
APP_PORT ?= $(shell sed -n 's/^APP_PORT=//p' .env 2>/dev/null | head -1)
APP_PORT := $(if $(APP_PORT),$(APP_PORT),8000)

docker-up:
	mkdir -p data workspace
	bash scripts/docker-preflight.sh
	docker compose up -d --build
	@echo ""
	@echo "起動しました。http://localhost:$(APP_PORT)"
	@echo "ポートを変えるには .env に  APP_PORT=9000  を書いて make docker-rebuild"
	@echo "初回はモデル取得に時間がかかります:  make docker-logs"

docker-check:
	bash scripts/docker-preflight.sh

docker-logs:
	docker compose logs -f app ollama-pull

docker-ps:
	docker compose ps

docker-down:
	docker compose down

docker-rebuild:
	bash scripts/docker-preflight.sh
	docker compose up -d --build app

# ---- Linux に常設（systemd + Docker）----------------------------------------
# systemd が「起動と停止の入口」、コンテナの restart: unless-stopped が自己回復。

service-install:
	bash scripts/install-service.sh

service-status:
	systemctl status biomni-hypo.service --no-pager || true
	@echo ""
	docker compose ps

service-logs:
	docker compose logs -f app

service-update:
	git pull
	sudo systemctl reload biomni-hypo.service

service-uninstall:
	bash scripts/uninstall-service.sh
