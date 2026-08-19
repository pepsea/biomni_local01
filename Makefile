.PHONY: help install install-min test lint notebook up api check-env doctor models fetch check \
	docker-up docker-down docker-logs docker-ps docker-rebuild

help:
	@echo "install      依存を全部入れる（biomni 含む・重い）"
	@echo "install-min  最小構成（テストとノートブック 03 まで動く）"
	@echo "test         pytest"
	@echo "lint         ruff"
	@echo "notebook     JupyterLab を起動"
	@echo "up           Web アプリを起動（環境確認つき）  ← いつもこれ"
	@echo "check-env    起動せず環境だけ確認"
	@echo "doctor       どの Python を使っているか診断（依存が入らないとき）"
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

docker-up:
	mkdir -p data workspace
	docker compose up -d --build
	@echo ""
	@echo "起動しました。http://localhost:8000"
	@echo "初回はモデル取得に時間がかかります:  make docker-logs"

docker-logs:
	docker compose logs -f app ollama-pull

docker-ps:
	docker compose ps

docker-down:
	docker compose down

docker-rebuild:
	docker compose up -d --build app
