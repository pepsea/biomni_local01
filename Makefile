.PHONY: help install install-min test lint notebook up api check-env models fetch check

help:
	@echo "install      依存を全部入れる（biomni 含む・重い）"
	@echo "install-min  最小構成（テストとノートブック 03 まで動く）"
	@echo "test         pytest"
	@echo "lint         ruff"
	@echo "notebook     JupyterLab を起動"
	@echo "up           Web アプリを起動（環境確認つき）  ← いつもこれ"
	@echo "check-env    起動せず環境だけ確認"
	@echo "api          uvicorn を直接起動（確認なし）"
	@echo "models       ローカルの Ollama にあるモデルを一覧"
	@echo "fetch        許可リストのデータセットを取得"
	@echo "check        lint + test"

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

api:
	uvicorn backend.app.main:app --reload --port 8000

models:
	python scripts/list_models.py

fetch:
	python scripts/fetch_datasets.py

check: lint test
