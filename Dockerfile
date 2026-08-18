FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY biomni_hypo ./biomni_hypo
COPY backend ./backend
COPY config ./config
COPY scripts ./scripts

RUN useradd -m -u 1000 app && mkdir -p /app/data /app/workspace && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
