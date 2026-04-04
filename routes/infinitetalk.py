import os
import tempfile
import shutil
import uuid
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse
import logging

logger = logging.getLogger(__name__)

# Video retention: delete after this many seconds (default: 1 hour)
VIDEO_RETENTION_SECONDS = int(os.environ.get("VIDEO_RETENTION_SECONDS", 3600))
# Cleanup interval: how often to check for old videos (default: 10 minutes)
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 600))

from config import settings
from clients import InfinitetalkS3Client

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=4)

# In-memory job store (includes creation timestamp for cleanup)
jobs: Dict[str, dict] = {}


def cleanup_old_videos():
    """
    Background thread that periodically removes old video files and job entries.
    """
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL_SECONDS)
            now = time.time()
            to_delete = []
            
            for job_id, job in list(jobs.items()):
                created_at = job.get("created_at", now)
                age = now - created_at
                
                # Delete if older than retention period
                if age > VIDEO_RETENTION_SECONDS:
                    output_path = job.get("output_path")
                    if output_path and os.path.exists(output_path):
                        try:
                            os.unlink(output_path)
                            logger.info(f"Cleaned up old video: {output_path}")
                        except Exception as e:
                            logger.warning(f"Failed to delete {output_path}: {e}")
                    to_delete.append(job_id)
            
            for job_id in to_delete:
                jobs.pop(job_id, None)
            
            if to_delete:
                logger.info(f"Cleaned up {len(to_delete)} old jobs")
                
        except Exception as e:
            logger.error(f"Cleanup thread error: {e}")


# Start cleanup thread on module load
_cleanup_thread = threading.Thread(target=cleanup_old_videos, daemon=True)
_cleanup_thread.start()


def get_client() -> InfinitetalkS3Client:
    return InfinitetalkS3Client(
        runpod_endpoint_id=settings.INFINITETALK_ENDPOINT_ID,
        runpod_api_key=settings.RUNPOD_API_KEY,
        s3_endpoint_url=settings.S3_ENDPOINT_URL,
        s3_access_key_id=settings.S3_ACCESS_KEY_ID,
        s3_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        s3_bucket_name=settings.S3_BUCKET_NAME,
        s3_region=settings.S3_REGION
    )


async def handle_upload(file: UploadFile, tmpdir: str) -> str:
    if not file:
        return None
    path = os.path.join(tmpdir, file.filename)
    with open(path, "wb") as f:
        content = await file.read()
        f.write(content)
    return path


def run_infinitetalk_job(
    job_id: str,
    media_path: str,
    audio_path: str,
    audio_path_2: Optional[str],
    prompt: str,
    width: int,
    height: int,
    person_count: str,
    input_type: str,
    use_network_volume: bool,
    tmpdir: str
):
    try:
        jobs[job_id]["status"] = "IN_PROGRESS"
        client = get_client()
        
        # For video input, we need to pass the media_path as image_path
        # The client will handle it correctly based on input_type
        result = client.create_video_from_files(
            image_path=media_path,
            audio_path=audio_path,
            audio_path_2=audio_path_2,
            prompt=prompt,
            width=width,
            height=height,
            person_count=person_count,
            input_type=input_type,
            use_network_volume=use_network_volume
        )
        
        if result.get("status") == "COMPLETED":
            out_path = f"outputs/{job_id}.mp4"
            os.makedirs("outputs", exist_ok=True)
            
            if client.save_video_result(result, out_path):
                jobs[job_id] = {
                    "status": "COMPLETED",
                    "output_path": out_path,
                    "error": None
                }
            else:
                jobs[job_id] = {
                    "status": "FAILED",
                    "output_path": None,
                    "error": "Failed to save video result"
                }
        else:
            jobs[job_id] = {
                "status": "FAILED",
                "output_path": None,
                "error": result.get("error", "Unknown error")
            }
    except Exception as e:
        jobs[job_id] = {
            "status": "FAILED",
            "output_path": None,
            "error": str(e)
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.post("/submit")
async def submit(
    image: UploadFile = File(None),
    video: UploadFile = File(None),
    audio: UploadFile = File(...),
    audio_2: UploadFile = File(None),
    prompt: str = Form(...),
    width: int = Form(544),
    height: int = Form(960),
    person_count: str = Form("single"),
    input_type: str = Form("image"),
    use_network_volume: bool = Form(False)
):
    # Validate input based on input_type
    if input_type == "image" and not image:
        raise HTTPException(status_code=400, detail="Image file required for image input type")
    if input_type == "video" and not video:
        raise HTTPException(status_code=400, detail="Video file required for video input type")
    if person_count == "multi" and not audio_2:
        raise HTTPException(status_code=400, detail="Second audio file required for multi-person workflow")
    
    # Validate file sizes (max 200MB each)
    MAX_SIZE = 200 * 1024 * 1024
    
    # Validate media file
    media_file = image if input_type == "image" else video
    media_content = await media_file.read()
    if len(media_content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"{input_type.capitalize()} file too large (max 200MB)")
    await media_file.seek(0)
    
    # Validate audio files
    audio_content = await audio.read()
    if len(audio_content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="Audio file too large (max 200MB)")
    await audio.seek(0)
    
    if audio_2:
        audio_2_content = await audio_2.read()
        if len(audio_2_content) > MAX_SIZE:
            raise HTTPException(status_code=413, detail="Second audio file too large (max 200MB)")
        await audio_2.seek(0)
    
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "IN_QUEUE",
        "output_path": None,
        "error": None,
        "created_at": time.time()
    }
    
    tmpdir = tempfile.mkdtemp()
    media_path = await handle_upload(media_file, tmpdir)
    audio_path = await handle_upload(audio, tmpdir)
    audio_path_2 = await handle_upload(audio_2, tmpdir) if audio_2 else None
    
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        executor,
        run_infinitetalk_job,
        job_id,
        media_path,
        audio_path,
        audio_path_2,
        prompt,
        width,
        height,
        person_count,
        input_type,
        use_network_volume,
        tmpdir
    )
    
    return {"job_id": job_id}


@router.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return {
        "status": job["status"],
        "error": job.get("error")
    }


@router.get("/download/{job_id}")
async def download(job_id: str, custom_name: str = None):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] != "COMPLETED":
        raise HTTPException(status_code=400, detail="Job not completed")
    
    output_path = job.get("output_path")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output file not found")
    
    # Use custom name if provided, otherwise default
    filename = custom_name if custom_name else f"infinitetalk_{job_id}.mp4"
    if not filename.endswith('.mp4'):
        filename += '.mp4'
    
    # Mark job as downloaded for potential earlier cleanup
    job["downloaded_at"] = time.time()
    
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename
    )
