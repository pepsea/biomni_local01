# 2 段構成。ビルド用のコンパイラを実行イメージに持ち込まない。
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
# 仮想環境に入れて、そのままコピーする（どの Python か迷う余地を無くす）
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim

# ホストのユーザーと UID を合わせる。
# ./data と ./workspace を bind マウントするので、ずれると書き込めない。
# 既定は 1000（多くの Linux で最初のユーザー）。
ARG APP_UID=1000
ARG APP_GID=1000

# curl は healthcheck 用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app 2>/dev/null || true \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" app 2>/dev/null \
       || useradd --create-home --uid "${APP_UID}" app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=app:app biomni_hypo ./biomni_hypo
COPY --chown=app:app backend ./backend
COPY --chown=app:app config ./config
COPY --chown=app:app scripts ./scripts

# データレイクとラン成果物はボリュームで受ける
RUN mkdir -p /app/data /app/workspace && chown -R app:app /app
USER app

EXPOSE 8000

# アプリ自身が「使えるモデルがあるか」まで見ている /api/health を使う
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
