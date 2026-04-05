# ── FastAPI server image ──────────────────────────────────────
FROM python:3.11-slim

# ffmpeg needed by pydub (bot uses it; keep in same image if co-deploying)
# curl needed for Docker health checks
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY config.py        ./config.py
COPY main.py          ./main.py
COPY bot.py           ./bot.py
COPY clients/         ./clients/
COPY routes/          ./routes/
COPY .env             ./.env

RUN mkdir -p outputs

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
