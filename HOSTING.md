# InfiniteTalk Bot — Hosting & Deployment Guide

## Architecture overview

```
User (Telegram)
      │  Telegram API (polling)
      ▼
┌─────────────┐     HTTP      ┌──────────────────────────┐
│  bot.py     │ ──────────▶  │  FastAPI  (main.py)      │
│  (Python)   │              │  /api/infinitetalk/…     │
└─────────────┘              └──────────┬───────────────┘
                                        │ Python (in-process)
                                        ▼
                             ┌──────────────────────────┐
                             │  InfinitetalkS3Client    │
                             │  ① upload files → S3     │
                             │  ② submit job → RunPod   │
                             │  ③ poll until done       │
                             │  ④ download result ← S3  │
                             └──────────────────────────┘
```

Both the bot and the API server run on **one cheap VPS**.
RunPod does all the heavy GPU work.

---

## Recommended hosting: Hetzner Cloud CX22

| Spec       | Value           |
|------------|-----------------|
| vCPU       | 2               |
| RAM        | 4 GB            |
| Storage    | 40 GB SSD       |
| Price      | ~€4/month       |
| Location   | Any (pick EU)   |

The server only coordinates jobs — RunPod handles GPU.
Even a 2 GB RAM VPS works; 4 GB gives comfortable headroom.

**Other good options:**
- DigitalOcean Droplet ($6/month)
- Vultr Cloud Compute ($6/month)
- AWS Lightsail ($5/month)
- Oracle Cloud Free Tier (always-free 1 GB ARM instance)

---

## One-command deploy (Docker Compose)

### 1. Provision the VPS

SSH in and install Docker:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Upload project files

```bash
# From your local machine:
scp -r ./infinitetalk_project root@YOUR_SERVER_IP:/opt/infinitetalk
```

Or clone from Git if you push it there.

### 3. Set your bot token in .env

```bash
nano /opt/infinitetalk/.env
# Set TELEGRAM_BOT_TOKEN=<token from @BotFather>
```

### 4. Start everything

```bash
cd /opt/infinitetalk
docker compose up -d --build
```

That's it. The API starts first; the bot waits for it to be healthy, then connects.

### 5. Check logs

```bash
docker compose logs -f api   # FastAPI logs (job progress)
docker compose logs -f bot   # Telegram bot logs
```

### 6. Restart / update

```bash
git pull   # if using git
docker compose up -d --build
```

---

## Running without Docker (development)

```bash
# Terminal 1 – FastAPI server
cd infinitetalk_project
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 – Telegram bot
cd infinitetalk_project
python bot.py
```

Both read from the same `.env` file in the project root.

---

## Getting your Telegram Bot Token

1. Open Telegram, search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
4. In BotFather: `/mybots → your bot → Bot Settings → Payments`
   — Stars payments work **without** connecting any payment provider.
   The provider token in the code is already set to `""` (empty).

---

## Environment variables reference

| Variable                   | Where used        | Description                          |
|----------------------------|-------------------|--------------------------------------|
| `TELEGRAM_BOT_TOKEN`       | bot.py            | From @BotFather                      |
| `INFINITETALK_ENDPOINT_ID` | FastAPI / client  | RunPod serverless endpoint ID        |
| `RUNPOD_API_KEY`           | FastAPI / client  | RunPod API key                       |
| `S3_ENDPOINT_URL`          | FastAPI / client  | RunPod S3-compatible storage URL     |
| `S3_ACCESS_KEY_ID`         | FastAPI / client  | S3 access key                        |
| `S3_SECRET_ACCESS_KEY`     | FastAPI / client  | S3 secret key                        |
| `S3_BUCKET_NAME`           | FastAPI / client  | Bucket to stage input/output files   |
| `S3_REGION`                | FastAPI / client  | Default: eu-ro-1                     |
| `INFINITETALK_API_BASE`    | bot.py only       | URL of the FastAPI server            |
| `VIDEO_RETENTION_SECONDS`  | FastAPI           | How long to keep videos (default: 3600 = 1 hour) |
| `CLEANUP_INTERVAL_SECONDS` | FastAPI           | Cleanup check interval (default: 600 = 10 min) |

`INFINITETALK_API_BASE` defaults to `http://localhost:8000`.
In Docker Compose it is overridden to `http://api:8000` automatically.

**Video cleanup:** Generated videos are automatically deleted after `VIDEO_RETENTION_SECONDS`.
This prevents disk from filling up. Adjust based on your usage patterns.

---

## Project file layout

```
infinitetalk_project/
├── .env                        ← credentials (never commit this)
├── config.py                   ← pydantic-settings loader
├── main.py                     ← FastAPI app entry point
├── bot.py                      ← Telegram bot entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── clients/
│   ├── __init__.py
│   └── infinitetalk_s3_client.py   ← your original S3 client
├── routes/
│   ├── __init__.py
│   └── infinitetalk.py             ← your original FastAPI router
└── outputs/                    ← generated videos (auto-created)
```

---

## Cost estimate

| Component              | Cost             |
|------------------------|------------------|
| Hetzner CX22 VPS       | ~€4 / month      |
| RunPod serverless GPU  | Pay-per-second   |
| RunPod S3 storage      | Minimal          |
| Telegram Stars         | User pays        |

RunPod charges only when a job runs — idle time costs nothing.
A typical 25-second Full HD generation on an A40 GPU costs ~$0.01–0.03.

---

## Security tips

- Keep `.env` out of git: add it to `.gitignore`
- Restrict the VPS firewall to only expose port 8000 internally
  (the bot connects via Docker internal DNS, not the public internet)
- Rotate your RunPod API key if you ever expose it accidentally
