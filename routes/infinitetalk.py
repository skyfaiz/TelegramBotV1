import os
import tempfile
import shutil
import uuid
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional
from io import BytesIO

from fastapi import APIRouter, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydub import AudioSegment
import logging
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)

# Video retention: delete after this many seconds (default: 1 hour)
VIDEO_RETENTION_SECONDS = int(os.environ.get("VIDEO_RETENTION_SECONDS", 3600))
# Cleanup interval: how often to check for old videos (default: 10 minutes)
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 600))

from config import settings
from clients import InfinitetalkS3Client

router = APIRouter()

# ── AWS Polly Configuration ─────────────────────────────────────────────────
POLLY_VOICES = {
    "standard": ["Joanna", "Matthew", "Ivy", "Kendra", "Kimberly", "Salli", "Joey", "Justin"],
    "neural": ["Joanna", "Matthew", "Ivy", "Kendra", "Kimberly", "Salli", "Joey", "Justin", "Ruth", "Stephen"],
    "generative": ["Ruth", "Matthew"],
}

# ── Pydantic models for TTS/Clone ───────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    engine: str = "neural"  # standard, neural, generative
    voice_id: str = "Joanna"

class TTSResponse(BaseModel):
    audio_path: str
    duration: float
    characters: int

class CloneRequest(BaseModel):
    text: str

class CloneResponse(BaseModel):
    audio_path: str
    duration: float
    characters: int

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


# ═══════════════════════════════════════════════════════════════════════════════
# Text-to-Speech (AWS Polly) Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

def get_polly_client():
    """Create AWS Polly client using settings."""
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        return None
    return boto3.client(
        'polly',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION
    )


@router.post("/tts", response_model=TTSResponse)
async def text_to_speech(request: TTSRequest):
    """
    Convert text to speech using AWS Polly.
    
    Engines:
    - standard: Basic TTS, cheapest
    - neural: Natural-sounding, mid-tier
    - generative: Most expressive, premium (limited voices)
    """
    # Validate engine
    engine = request.engine.lower()
    if engine not in POLLY_VOICES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid engine. Choose from: {list(POLLY_VOICES.keys())}"
        )
    
    # Validate voice for engine
    if request.voice_id not in POLLY_VOICES[engine]:
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{request.voice_id}' not available for {engine} engine. "
                   f"Available: {POLLY_VOICES[engine]}"
        )
    
    # Validate text length (Polly limit: 3000 chars for standard, 6000 for neural/generative)
    max_chars = 3000 if engine == "standard" else 6000
    if len(request.text) > max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(request.text)} chars). Max for {engine}: {max_chars}"
        )
    
    polly = get_polly_client()
    if not polly:
        raise HTTPException(
            status_code=503,
            detail="AWS Polly not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        )
    
    try:
        # Map engine to Polly engine parameter
        engine_map = {
            "standard": "standard",
            "neural": "neural",
            "generative": "generative"
        }
        
        response = polly.synthesize_speech(
            Text=request.text,
            OutputFormat="mp3",
            VoiceId=request.voice_id,
            Engine=engine_map[engine]
        )
        
        # Read audio stream
        audio_stream = response['AudioStream'].read()
        
        # Save to temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", dir="outputs")
        tmp.write(audio_stream)
        tmp.close()
        
        # Calculate duration using pydub
        audio_seg = AudioSegment.from_mp3(tmp.name)
        duration = len(audio_seg) / 1000.0  # Convert ms to seconds
        
        logger.info(f"TTS generated: {len(request.text)} chars, {duration:.1f}s, voice={request.voice_id}")
        
        return TTSResponse(
            audio_path=tmp.name,
            duration=duration,
            characters=len(request.text)
        )
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        logger.error(f"Polly error: {error_code} - {error_msg}")
        raise HTTPException(status_code=500, detail=f"Polly error: {error_msg}")
    except NoCredentialsError:
        raise HTTPException(status_code=503, detail="AWS credentials not configured")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tts/voices")
async def get_tts_voices():
    """Return available voices for each engine."""
    return POLLY_VOICES


# ═══════════════════════════════════════════════════════════════════════════════
# Voice Cloning Endpoint (Placeholder)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/clone", response_model=CloneResponse)
async def clone_voice(
    reference_audio: UploadFile = File(...),
    text: str = Form(...)
):
    """
    Clone a voice from reference audio and generate speech.
    
    NOTE: This is a placeholder endpoint. AWS Polly's "Personal Voice" feature
    requires enterprise approval. This endpoint currently returns a mock response.
    
    Future implementation options:
    - AWS Polly Personal Voice (enterprise)
    - ElevenLabs Voice Cloning API
    - Coqui TTS (open source)
    """
    # Validate text length
    if len(text) > 1000:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(text)} chars). Max: 1000"
        )
    
    # Validate reference audio size (max 10MB)
    content = await reference_audio.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Reference audio too large (max 10MB)"
        )
    
    # Save reference audio temporarily (for future implementation)
    ref_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", dir="outputs")
    ref_tmp.write(content)
    ref_tmp.close()
    
    try:
        # ═══════════════════════════════════════════════════════════════════════
        # PLACEHOLDER: Voice cloning not yet implemented
        # For now, return an error explaining the feature is coming soon
        # ═══════════════════════════════════════════════════════════════════════
        
        # Clean up reference audio
        os.unlink(ref_tmp.name)
        
        raise HTTPException(
            status_code=501,
            detail="Voice cloning coming soon! AWS Polly Personal Voice requires "
                   "enterprise approval. Contact admin for updates."
        )
        
        # ═══════════════════════════════════════════════════════════════════════
        # Future implementation would look like:
        # 
        # 1. Upload reference audio to cloning service
        # 2. Generate speech with cloned voice
        # 3. Save and return audio file
        #
        # return CloneResponse(
        #     audio_path=output_path,
        #     duration=duration,
        #     characters=len(text)
        # )
        # ═══════════════════════════════════════════════════════════════════════
        
    except HTTPException:
        raise
    except Exception as e:
        # Clean up on error
        if os.path.exists(ref_tmp.name):
            os.unlink(ref_tmp.name)
        logger.error(f"Clone error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
