"""
main.py  –  InfiniteTalk FastAPI server
=======================================
Exposes the InfiniteTalk video-generation router under /api/infinitetalk.
The Telegram bot talks to this server to submit jobs, poll status,
and download completed videos.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes import infinitetalk

app = FastAPI(
    title="InfiniteTalk API",
    description="Talking-head video generation via RunPod + InfiniteTalk",
    version="1.0.0",
)

# CORS – only needs to allow the bot's internal requests (localhost),
# but kept permissive for flexibility.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the infinitetalk router (submit / status / download)
app.include_router(
    infinitetalk.router,
    prefix="/api/infinitetalk",
    tags=["InfiniteTalk"],
)


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "InfiniteTalk API",
        "docs": "/docs",
        "submit":   "/api/infinitetalk/submit",
        "status":   "/api/infinitetalk/status/{job_id}",
        "download": "/api/infinitetalk/download/{job_id}",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
