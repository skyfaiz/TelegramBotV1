"""
bot.py  –  InfiniteTalk Telegram Bot (Telegram Stars payments)
==============================================================
Conversation flow:
  /start → photo → audio (auto-trim >25 s) → prompt
         → orientation → resolution → Stars invoice → generate → deliver

The bot calls the local FastAPI server:
  POST /api/infinitetalk/submit
  GET  /api/infinitetalk/status/{job_id}
  GET  /api/infinitetalk/download/{job_id}

Which in turn calls InfinitetalkS3Client → RunPod → S3.

Run:
    python bot.py
"""

import os
import math
import uuid
import asyncio
import logging
import tempfile
import requests
from io import BytesIO
from pydub import AudioSegment

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ── Load .env ────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Settings (read from environment / .env) ──────────────────────────────────
BOT_TOKEN             = os.environ["TELEGRAM_BOT_TOKEN"]
INFINITETALK_API_BASE = os.environ.get("INFINITETALK_API_BASE", "http://localhost:8000")
MAX_AUDIO_SECONDS     = {"sd": 30, "hd": 20}  # Max audio length per tier
FREE_MODE_PASSWORD    = "HighKGP"
ONE_TIME_CODE         = "DEMO5S"  # One-time 5s SD generation code

# ── Pricing in Telegram Stars (XTR) ─────────────────────────────────────────
# 1 Star ≈ ₹1.50  (Telegram: 100 Stars ≈ $1.99 ≈ ₹165 at ₹83/$)
# ₹200/min ÷ ₹1.50/Star ≈ 133 Stars/min  → rounded to 130
# ₹120/min → 80 Stars/min
# ₹ 60/min → 40 Stars/min
STARS_PER_MINUTE = {"hd": 80, "sd": 40}
TIER_LABEL        = {"hd": "HD (720p)", "sd": "SD (480p)"}
INR_PER_STAR      = 1.50   # display only; actual rate set by Telegram

# ── Resolutions (Wan2.1-compatible – all multiples of 64) ───────────────────
RESOLUTIONS = {
    "portrait_sd":   {"w": 480,  "h": 832,  "tier": "sd",  "label": "📱 Portrait SD  (480×832)"},
    "portrait_hd":   {"w": 720,  "h": 1280, "tier": "hd",  "label": "📱 Portrait HD  (720×1280)"},
    "landscape_sd":  {"w": 832,  "h": 480,  "tier": "sd",  "label": "� Landscape SD  (832×480)"},
    "landscape_hd":  {"w": 1280, "h": 720,  "tier": "hd",  "label": "🖥 Landscape HD  (1280×720)"},
    "square_sd":     {"w": 480,  "h": 480,  "tier": "sd",  "label": "⬜ Square SD  (480×480)"},
    "square_hd":     {"w": 768,  "h": 768,  "tier": "hd",  "label": "⬜ Square HD  (768×768)"},
}

# ── GPU time estimate: seconds of compute per second of video ────────────────
GENERATION_SPEED = {"sd": 6, "hd": 10}

# ── Conversation states ──────────────────────────────────────────────────────
(
    STATE_IMAGE,
    STATE_CHOOSE_AUDIO_METHOD,  # New: choose upload/TTS/clone
    STATE_AUDIO,                # Upload audio file
    STATE_TTS_TEXT,             # TTS: enter text
    STATE_TTS_ENGINE,           # TTS: choose engine
    STATE_TTS_VOICE,            # TTS: choose voice
    STATE_CLONE_TEXT,           # Clone: enter text
    STATE_CLONE_AUDIO,          # Clone: upload reference voice
    STATE_PROMPT,
    STATE_ORIENTATION,
    STATE_RESOLUTION,
    STATE_CONFIRM,
) = range(12)

# ── TTS Pricing (Stars per 100 characters) ───────────────────────────────────
TTS_STARS_PER_100_CHARS = {"standard": 0, "neural": 2, "generative": 5}
CLONE_STARS_PER_100_CHARS = 10  # Premium feature

# ── Available TTS Voices ─────────────────────────────────────────────────────
TTS_VOICES = {
    "standard": [
        ("Joanna", "🇺🇸 Joanna (Female)"),
        ("Matthew", "🇺🇸 Matthew (Male)"),
        ("Ivy", "🇺🇸 Ivy (Child)"),
        ("Kendra", "🇺🇸 Kendra (Female)"),
        ("Joey", "🇺🇸 Joey (Male)"),
    ],
    "neural": [
        ("Joanna", "🇺🇸 Joanna (Female)"),
        ("Matthew", "🇺🇸 Matthew (Male)"),
        ("Ivy", "🇺🇸 Ivy (Child)"),
        ("Ruth", "🇺🇸 Ruth (Female)"),
        ("Stephen", "🇺🇸 Stephen (Male)"),
    ],
    "generative": [
        ("Ruth", "🇺🇸 Ruth (Female)"),
        ("Matthew", "🇺🇸 Matthew (Male)"),
    ],
}

logging.basicConfig(
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════════

def stars_for_job(audio_s: float, tier: str) -> int:
    """Calculate stars cost with minimum of 10 stars."""
    return max(10, math.ceil(audio_s / 60.0 * STARS_PER_MINUTE[tier]))

def estimate_wait(audio_s: float, tier: str) -> int:
    return math.ceil(audio_s * GENERATION_SPEED[tier])

def fmt_wait(secs: int) -> str:
    if secs < 60:
        return f"~{secs} sec"
    m, s = divmod(secs, 60)
    return f"~{m} min {s} sec" if s else f"~{m} min"

def crop_audio(raw: bytes, fmt: str, tier: str = "sd") -> tuple[bytes, float]:
    """
    Decode audio, trim to tier-specific max length if needed,
    re-encode as MP3, return (mp3_bytes, actual_duration_s).
    """
    max_seconds = MAX_AUDIO_SECONDS.get(tier, 30)
    seg = AudioSegment.from_file(BytesIO(raw), format=fmt)
    dur = len(seg) / 1000.0
    out = seg[: max_seconds * 1000] if dur > max_seconds else seg
    buf = BytesIO()
    out.export(buf, format="mp3")
    return buf.getvalue(), min(dur, max_seconds)


# ═══════════════════════════════════════════════════════════════════════════════
# Keyboard builders
# ═══════════════════════════════════════════════════════════════════════════════

def audio_method_kb() -> InlineKeyboardMarkup:
    """Keyboard for choosing audio input method."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Upload Audio File", callback_data="audio_upload")],
        [InlineKeyboardButton("📝 Text-to-Speech (TTS)", callback_data="audio_tts")],
        [InlineKeyboardButton("🗣️ Clone My Voice", callback_data="audio_clone")],
    ])


def tts_engine_kb() -> InlineKeyboardMarkup:
    """Keyboard for choosing TTS engine."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Standard (Free)", callback_data="tts_engine_standard")],
        [InlineKeyboardButton("🎯 Neural (2⭐/100 chars)", callback_data="tts_engine_neural")],
        [InlineKeyboardButton("✨ Generative (5⭐/100 chars)", callback_data="tts_engine_generative")],
    ])


def tts_voice_kb(engine: str) -> InlineKeyboardMarkup:
    """Keyboard for choosing TTS voice based on engine."""
    voices = TTS_VOICES.get(engine, TTS_VOICES["neural"])
    rows = [
        [InlineKeyboardButton(label, callback_data=f"tts_voice_{voice_id}")]
        for voice_id, label in voices
    ]
    return InlineKeyboardMarkup(rows)


def orientation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 Portrait",  callback_data="orient_portrait"),
        InlineKeyboardButton("🖥 Landscape", callback_data="orient_landscape"),
        InlineKeyboardButton("⬜ Square",    callback_data="orient_square"),
    ]])

def resolution_kb(orientation: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(RESOLUTIONS[k]["label"], callback_data=k)]
        for k in RESOLUTIONS if k.startswith(orientation)
    ]
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI client calls  (blocking – wrapped in run_in_executor)
# ═══════════════════════════════════════════════════════════════════════════════

def _api_submit(image_path: str, audio_path: str, prompt: str,
                width: int, height: int) -> str | None:
    """
    POST /api/infinitetalk/submit
    Returns internal job_id or None on failure.
    """
    url = f"{INFINITETALK_API_BASE}/api/infinitetalk/submit"
    try:
        with open(image_path, "rb") as img, open(audio_path, "rb") as aud:
            resp = requests.post(
                url,
                files={
                    "image": ("image.jpg", img, "image/jpeg"),
                    "audio": ("audio.mp3", aud, "audio/mpeg"),
                },
                data={
                    "prompt":             prompt,
                    "width":              str(width),
                    "height":             str(height),
                    "person_count":       "single",
                    "input_type":         "image",
                    "use_network_volume": "false",
                },
                timeout=60,
            )
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
        logger.info("API submit OK – job_id=%s", job_id)
        return job_id
    except Exception as exc:
        logger.error("_api_submit error: %s", exc)
        return None


def _api_poll(job_id: str, timeout: int = 1800, interval: int = 10) -> dict:
    """
    GET /api/infinitetalk/status/{job_id}
    Polls until COMPLETED / FAILED / TIMEOUT.
    """
    import time
    url      = f"{INFINITETALK_API_BASE}/api/infinitetalk/status/{job_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data   = requests.get(url, timeout=15).json()
            status = data.get("status", "")
            logger.info("job %s status: %s", job_id, status)
            if status in ("COMPLETED", "FAILED"):
                return data
        except Exception as exc:
            logger.warning("_api_poll error: %s", exc)
        time.sleep(interval)
    return {"status": "TIMEOUT", "error": "Timed out waiting for generation"}


def _api_download(job_id: str) -> bytes | None:
    """
    GET /api/infinitetalk/download/{job_id}
    Returns raw MP4 bytes or None.
    """
    url = f"{INFINITETALK_API_BASE}/api/infinitetalk/download/{job_id}"
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.error("_api_download error: %s", exc)
        return None


def _api_tts(text: str, engine: str, voice_id: str) -> dict | None:
    """
    POST /api/infinitetalk/tts
    Returns {audio_path, duration, characters} or None on failure.
    """
    url = f"{INFINITETALK_API_BASE}/api/infinitetalk/tts"
    try:
        resp = requests.post(
            url,
            json={"text": text, "engine": engine, "voice_id": voice_id},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("_api_tts error: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Preserve free_mode flags when clearing user data
    free_mode = ctx.user_data.get("free_mode_permanent", False)
    demo_used = ctx.user_data.get("demo_used", False)
    ctx.user_data.clear()
    if free_mode:
        ctx.user_data["free_mode"] = True
        ctx.user_data["free_mode_permanent"] = True
    if demo_used:
        ctx.user_data["demo_used"] = True
    await update.message.reply_text(
        "🎬 *InfiniteTalk Video Bot*\n\n"
        "Turn a photo + voice clip into a talking-head video.\n"
        "Payment via ⭐ *Telegram Stars* — fast, no sign-ups.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📸 *Step 1* — Send me the *face photo*.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_IMAGE


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    is_free = ctx.user_data.get("free_mode") or ctx.user_data.get("free_mode_permanent")
    free_status = "✅ *FREE MODE ACTIVE*\n" if is_free else ""
    await update.message.reply_text(
        f"*InfiniteTalk Bot – Help*\n{free_status}\n"
        "1️⃣ Send a face *photo*\n"
        "2️⃣ Choose audio method:\n"
        "   • 🎵 *Upload* your own audio\n"
        "   • 📝 *Text-to-Speech* (AI voice)\n"
        "   • 🗣️ *Clone Voice* (coming soon)\n"
        "3️⃣ Type a *prompt* (speaking style)\n"
        "4️⃣ Choose *orientation* and *resolution*\n"
        "5️⃣ Pay with ⭐ *Telegram Stars*\n"
        "6️⃣ Receive your *video*!\n\n"
        "*Video pricing:*\n"
        "  SD (480p)  →  40 ⭐ / min\n"
        "  HD (720p)  →  80 ⭐ / min\n\n"
        "*TTS pricing (added to video cost):*\n"
        "  Standard   →  FREE\n"
        "  Neural     →  2 ⭐ / 100 chars\n"
        "  Generative →  5 ⭐ / 100 chars\n\n"
        "1 ⭐ ≈ ₹1.50 (Telegram sets the actual rate)\n\n"
        "/start  – begin a new video\n"
        "/cancel – abort current session",
        parse_mode="Markdown",
    )


# ── /free <password> — activate free mode ─────────────────────────────────────

async def cmd_free(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if len(args) != 1:
        await update.message.reply_text(
            "Usage: `/free <password>`\n\n"
            "Or use: `/free DEMO5S` for one 5-second SD video",
            parse_mode="Markdown"
        )
        return
    
    password = args[0]
    if password == FREE_MODE_PASSWORD:
        ctx.user_data["free_mode"] = True
        ctx.user_data["free_mode_permanent"] = True
        await update.message.reply_text(
            "🎉 *FREE MODE ACTIVATED!*\n\n"
            "You can now generate unlimited videos without payment.\n"
            "Use /start to begin.",
            parse_mode="Markdown",
        )
    elif password == ONE_TIME_CODE:
        if ctx.user_data.get("demo_used"):
            await update.message.reply_text(
                "❌ Demo code already used. Contact admin for more access.",
                parse_mode="Markdown"
            )
        else:
            ctx.user_data["demo_mode"] = True
            ctx.user_data["demo_used"] = True
            await update.message.reply_text(
                "🎁 *DEMO MODE ACTIVATED!*\n\n"
                "You can generate ONE video (max 5s, SD quality only).\n"
                "Use /start to begin.",
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            "❌ Incorrect password.", parse_mode="Markdown"
        )


# ── Step 1 : Image ───────────────────────────────────────────────────────────

async def receive_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    if msg.photo:
        file_obj = msg.photo[-1]          # highest resolution
    elif msg.document:
        file_obj = msg.document
    else:
        await msg.reply_text("❗ Please send a *photo* or image file.", parse_mode="Markdown")
        return STATE_IMAGE

    tg_file = await file_obj.get_file()
    data    = await tg_file.download_as_bytearray()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp.write(data); tmp.close()
    ctx.user_data["image_path"] = tmp.name

    await msg.reply_text(
        "✅ Photo saved!\n\n"
        "🎵 *Step 2* — Choose how to provide *audio*:",
        parse_mode="Markdown",
        reply_markup=audio_method_kb(),
    )
    return STATE_CHOOSE_AUDIO_METHOD


# ── Step 2a : Choose Audio Method ─────────────────────────────────────────────

async def choose_audio_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    
    method = q.data  # audio_upload, audio_tts, audio_clone
    ctx.user_data["audio_method"] = method
    
    if method == "audio_upload":
        await q.edit_message_text(
            "🎵 *Upload Audio*\n\n"
            "Send a *voice message or audio file*.\n"
            "_HD: max 20s, SD: max 30s — longer clips are trimmed automatically._",
            parse_mode="Markdown",
        )
        return STATE_AUDIO
    
    elif method == "audio_tts":
        await q.edit_message_text(
            "📝 *Text-to-Speech*\n\n"
            "Type the text you want the face to speak.\n"
            "_Max 3000 characters for Standard, 6000 for Neural/Generative._\n\n"
            "Example: _Hello! Welcome to my channel. Today we'll discuss..._",
            parse_mode="Markdown",
        )
        return STATE_TTS_TEXT
    
    elif method == "audio_clone":
        await q.edit_message_text(
            "🗣️ *Voice Cloning*\n\n"
            "⚠️ *Coming Soon!*\n\n"
            "Voice cloning requires AWS Polly Personal Voice (enterprise feature).\n"
            "Contact admin for updates on availability.\n\n"
            "For now, please use *Upload Audio* or *Text-to-Speech*.",
            parse_mode="Markdown",
            reply_markup=audio_method_kb(),
        )
        return STATE_CHOOSE_AUDIO_METHOD
    
    return STATE_CHOOSE_AUDIO_METHOD


# ── Step 2b : TTS - Enter Text ────────────────────────────────────────────────

async def receive_tts_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❗ Please type some text.")
        return STATE_TTS_TEXT
    
    if len(text) > 6000:
        await update.message.reply_text(
            f"❌ Text too long ({len(text)} chars). Max: 6000 characters.\n"
            "Please shorten your text.",
            parse_mode="Markdown"
        )
        return STATE_TTS_TEXT
    
    ctx.user_data["tts_text"] = text
    ctx.user_data["tts_characters"] = len(text)
    
    await update.message.reply_text(
        f"✅ Text received ({len(text)} characters)\n\n"
        "🎯 *Choose TTS Engine:*\n\n"
        "• *Standard* — Basic quality, FREE\n"
        "• *Neural* — Natural sound, 2⭐/100 chars\n"
        "• *Generative* — Most expressive, 5⭐/100 chars",
        parse_mode="Markdown",
        reply_markup=tts_engine_kb(),
    )
    return STATE_TTS_ENGINE


# ── Step 2c : TTS - Choose Engine ─────────────────────────────────────────────

async def choose_tts_engine(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    
    engine = q.data.replace("tts_engine_", "")  # standard, neural, generative
    ctx.user_data["tts_engine"] = engine
    
    # Calculate TTS cost preview
    chars = ctx.user_data.get("tts_characters", 0)
    tts_stars = math.ceil(chars / 100.0 * TTS_STARS_PER_100_CHARS[engine])
    
    await q.edit_message_text(
        f"Engine: *{engine.capitalize()}*\n"
        f"TTS Cost: *{tts_stars} ⭐* ({chars} chars)\n\n"
        "🎤 *Choose a voice:*",
        parse_mode="Markdown",
        reply_markup=tts_voice_kb(engine),
    )
    return STATE_TTS_VOICE


# ── Step 2d : TTS - Choose Voice → Generate Audio ────────────────────────────

async def choose_tts_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    
    voice_id = q.data.replace("tts_voice_", "")
    ctx.user_data["tts_voice"] = voice_id
    
    # Show generating message
    await q.edit_message_text(
        f"🔊 *Generating audio...*\n\n"
        f"Voice: {voice_id}\n"
        f"Engine: {ctx.user_data['tts_engine'].capitalize()}\n"
        f"Characters: {ctx.user_data['tts_characters']}",
        parse_mode="Markdown",
    )
    
    # Call TTS API
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _api_tts,
        ctx.user_data["tts_text"],
        ctx.user_data["tts_engine"],
        voice_id,
    )
    
    if not result:
        await q.message.reply_text(
            "❌ *TTS generation failed.*\n\n"
            "Please try again or use Upload Audio instead.\n"
            "Use /start to begin again.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    
    # Store audio info (same structure as upload flow)
    ctx.user_data["audio_path_raw"] = result["audio_path"]
    ctx.user_data["audio_duration_raw"] = result["duration"]
    ctx.user_data["audio_format"] = "mp3"
    
    await q.message.reply_text(
        f"✅ Audio generated — *{result['duration']:.1f} s*\n\n"
        "📝 *Step 3* — Type a *prompt* describing the speaking style.\n"
        "_e.g. 'speak naturally with slight head movement and eye contact'_",
        parse_mode="Markdown",
    )
    return STATE_PROMPT


# ── Step 2 : Audio ───────────────────────────────────────────────────────────

async def receive_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    if msg.voice:
        file_obj, fmt = msg.voice, "ogg"
    elif msg.audio:
        file_obj = msg.audio
        fmt = (msg.audio.file_name or "audio.mp3").rsplit(".", 1)[-1].lower()
    elif msg.document:
        file_obj = msg.document
        fmt = (msg.document.file_name or "audio.mp3").rsplit(".", 1)[-1].lower()
    else:
        await msg.reply_text("❗ Please send a *voice message or audio file*.", parse_mode="Markdown")
        return STATE_AUDIO

    tg_file = await file_obj.get_file()
    raw     = bytes(await tg_file.download_as_bytearray())

    try:
        # Store raw audio and format, will crop based on tier selection later
        seg = AudioSegment.from_file(BytesIO(raw), format=fmt)
        duration = len(seg) / 1000.0
        
        # Save raw audio temporarily
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        seg.export(tmp, format="mp3")
        tmp.close()
        
        ctx.user_data["audio_path_raw"] = tmp.name
        ctx.user_data["audio_duration_raw"] = duration
        ctx.user_data["audio_format"] = fmt
        
    except Exception as exc:
        logger.error("Audio processing failed: %s", exc)
        await msg.reply_text(
            "❌ Could not read that audio file.\n"
            "Please try MP3, OGG, WAV, or send a voice message."
        )
        return STATE_AUDIO

    await msg.reply_text(
        f"✅ Audio ready — *{duration:.1f} s*\n\n"
        "📝 *Step 3* — Type a *prompt* describing the speaking style.\n"
        "_e.g. 'speak naturally with slight head movement and eye contact'_",
        parse_mode="Markdown",
    )
    return STATE_PROMPT


# ── Step 3 : Prompt ──────────────────────────────────────────────────────────

async def receive_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = update.message.text.strip()
    if not prompt:
        await update.message.reply_text("❗ Please type a prompt.")
        return STATE_PROMPT
    ctx.user_data["prompt"] = prompt
    await update.message.reply_text(
        "✅ Prompt saved!\n\n🖼 *Step 4* — Choose video *orientation*:",
        parse_mode="Markdown",
        reply_markup=orientation_kb(),
    )
    return STATE_ORIENTATION


# ── Step 4 : Orientation ─────────────────────────────────────────────────────

async def choose_orientation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    orientation = q.data.replace("orient_", "")
    ctx.user_data["orientation"] = orientation
    await q.edit_message_text(
        f"Orientation: *{orientation.capitalize()}*\n\n"
        "🎞 *Step 5* — Choose *resolution*\n"
        "_(higher quality costs more ⭐ Stars)_",
        parse_mode="Markdown",
        reply_markup=resolution_kb(orientation),
    )
    return STATE_RESOLUTION


# ── Step 5 : Resolution → send Stars invoice ─────────────────────────────────

async def choose_resolution(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    res_key = q.data
    res     = RESOLUTIONS[res_key]
    tier    = res["tier"]
    
    # Crop audio based on tier-specific max length
    raw_audio_path = ctx.user_data["audio_path_raw"]
    raw_duration = ctx.user_data["audio_duration_raw"]
    max_seconds = MAX_AUDIO_SECONDS[tier]
    
    # Read and crop audio
    with open(raw_audio_path, "rb") as f:
        raw_audio = f.read()
    
    audio_bytes, dur = crop_audio(raw_audio, "mp3", tier)
    
    # Save cropped audio
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(audio_bytes); tmp.close()
    ctx.user_data["audio_path"] = tmp.name
    ctx.user_data["audio_duration"] = dur
    
    # Calculate video generation cost
    video_stars = stars_for_job(dur, tier)
    wait_s = estimate_wait(dur, tier)
    
    # Calculate TTS cost if applicable
    tts_stars = 0
    audio_method = ctx.user_data.get("audio_method", "audio_upload")
    if audio_method == "audio_tts":
        tts_engine = ctx.user_data.get("tts_engine", "standard")
        tts_chars = ctx.user_data.get("tts_characters", 0)
        tts_stars = math.ceil(tts_chars / 100.0 * TTS_STARS_PER_100_CHARS[tts_engine])
    elif audio_method == "audio_clone":
        clone_chars = ctx.user_data.get("clone_characters", 0)
        tts_stars = math.ceil(clone_chars / 100.0 * CLONE_STARS_PER_100_CHARS)
    
    # Total stars
    total_stars = video_stars + tts_stars

    ctx.user_data.update({
        "resolution_key": res_key,
        "width":          res["w"],
        "height":         res["h"],
        "tier":           tier,
        "video_stars":    video_stars,
        "tts_stars":      tts_stars,
        "stars_due":      total_stars,
        "wait_seconds":   wait_s,
    })

    # Unique payload ties this payment to this session
    payload = f"job_{uuid.uuid4().hex}"
    ctx.user_data["invoice_payload"] = payload

    inr_approx = round(total_stars * INR_PER_STAR)

    # Check if user has free mode or demo mode activated
    is_free = ctx.user_data.get("free_mode") or ctx.user_data.get("free_mode_permanent")
    is_demo = ctx.user_data.get("demo_mode")
    
    # Build cost breakdown string
    if tts_stars > 0:
        cost_breakdown = (
            f"🎥 Video      : {video_stars} ⭐\n"
            f"🗣️ AI Voice   : {tts_stars} ⭐\n"
            f"💰 Total      : *{total_stars} ⭐*  (~₹{inr_approx})"
        )
    else:
        cost_breakdown = f"💰 Cost       : *{total_stars} ⭐ Stars*  (~₹{inr_approx})"
    
    if is_demo:
        # Demo mode: enforce 5s SD only
        if tier != "sd":
            await q.answer("❌ Demo mode: SD quality only!", show_alert=True)
            return STATE_RESOLUTION
        if dur > 5:
            await q.answer("❌ Demo mode: max 5 seconds!", show_alert=True)
            return STATE_RESOLUTION
        
        # Deactivate demo mode after use
        ctx.user_data["demo_mode"] = False
        
        await q.edit_message_text(
            "*Order Summary* 🎁 DEMO MODE\n\n"
            f"🖼 Resolution : {res['label']}\n"
            f"💎 Quality    : {TIER_LABEL[tier]}\n"
            f"🎵 Audio      : {dur:.1f} s\n"
            f"⏱ Est. wait  : {fmt_wait(wait_s)}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Cost       : *FREE DEMO* (normally {total_stars} ⭐)\n\n"
            "🚀 *Generation starting automatically...*",
            parse_mode="Markdown",
        )
        ctx.application.create_task(
            _generate_and_deliver(q.message.chat_id, dict(ctx.user_data), ctx)
        )
        return ConversationHandler.END
    
    if is_free:
        await q.edit_message_text(
            "*Order Summary* 🎁 FREE MODE\n\n"
            f"🖼 Resolution : {res['label']}\n"
            f"💎 Quality    : {TIER_LABEL[tier]}\n"
            f"🎵 Audio      : {dur:.1f} s\n"
            f"⏱ Est. wait  : {fmt_wait(wait_s)}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Cost       : *FREE* (normally {total_stars} ⭐)\n\n"
            "🚀 *Generation starting automatically...*",
            parse_mode="Markdown",
        )
        # Skip payment, start generation directly
        ctx.application.create_task(
            _generate_and_deliver(q.message.chat_id, dict(ctx.user_data), ctx)
        )
        return ConversationHandler.END

    await q.edit_message_text(
        "*Order Summary*\n\n"
        f"🖼 Resolution : {res['label']}\n"
        f"💎 Quality    : {TIER_LABEL[tier]}\n"
        f"🎵 Audio      : {dur:.1f} s\n"
        f"⏱ Est. wait  : {fmt_wait(wait_s)}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{cost_breakdown}\n\n"
        "Tap *Pay* on the invoice below to start generation! 👇",
        parse_mode="Markdown",
    )

    # Build invoice description
    if tts_stars > 0:
        invoice_desc = f"{TIER_LABEL[tier]} · {res['label']} · TTS {dur:.1f}s"
    else:
        invoice_desc = f"{TIER_LABEL[tier]} · {res['label']} · {dur:.1f} s audio"

    # Send the native Telegram Stars invoice
    # provider_token="" means XTR (Stars) — no external payment provider needed
    await ctx.bot.send_invoice(
        chat_id     = q.message.chat_id,
        title       = "🎬 InfiniteTalk Video",
        description = invoice_desc,
        payload     = payload,
        provider_token = "",          # Empty = Telegram Stars
        currency    = "XTR",
        prices      = [LabeledPrice(label="Video generation", amount=total_stars)],
    )
    return STATE_CONFIRM


# ── Pre-checkout: Telegram asks us to approve before charging ────────────────

async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pcq = update.pre_checkout_query
    if pcq.invoice_payload == ctx.user_data.get("invoice_payload"):
        await pcq.answer(ok=True)
    else:
        await pcq.answer(
            ok=False,
            error_message="Session expired — please start again with /start.",
        )


# ── Payment confirmed: kick off background generation ────────────────────────

async def payment_success(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg     = update.message
    payment = msg.successful_payment

    # Always persist charge ID for potential refunds
    ctx.user_data["charge_id"] = payment.telegram_payment_charge_id

    stars  = payment.total_amount
    wait_s = ctx.user_data.get("wait_seconds", 60)

    await msg.reply_text(
        f"✅ *Payment confirmed!* ({stars} ⭐ Stars)\n\n"
        f"🚀 Generation started.\n"
        f"⏳ *Estimated wait: {fmt_wait(wait_s)}*\n\n"
        "I'll send the video here as soon as it's done.\n"
        "You can keep using Telegram normally while you wait! 🎬",
        parse_mode="Markdown",
    )

    ctx.application.create_task(
        _generate_and_deliver(msg.chat_id, dict(ctx.user_data), ctx)
    )
    return ConversationHandler.END


# ── Background task: generate via FastAPI → deliver video ────────────────────

async def _generate_and_deliver(
    chat_id: int,
    ud: dict,
    ctx: ContextTypes.DEFAULT_TYPE,
):
    bot  = ctx.application.bot
    loop = asyncio.get_event_loop()

    try:
        # ── 1. Submit job to FastAPI (which calls InfinitetalkS3Client) ──────
        job_id = await loop.run_in_executor(
            None, _api_submit,
            ud["image_path"], ud["audio_path"],
            ud["prompt"], ud["width"], ud["height"],
        )

        if not job_id:
            await bot.send_message(
                chat_id,
                "❌ *Job submission failed.*\n\n"
                f"Your Stars have been charged. Please contact support.\n"
                f"Charge ID: `{ud.get('charge_id', 'N/A')}`",
                parse_mode="Markdown",
            )
            return

        await bot.send_message(
            chat_id,
            f"✅ Job submitted to RunPod.\n"
            f"⏳ Processing… _{fmt_wait(ud['wait_seconds'])} estimated._\n\n"
            "_I'll notify you when it's done — no need to wait here._",
            parse_mode="Markdown",
        )

        # ── 2. Poll status (FastAPI polls RunPod internally) ─────────────────
        result = await loop.run_in_executor(None, _api_poll, job_id)

        if result.get("status") != "COMPLETED":
            err = result.get("error", "Unknown error")
            await bot.send_message(
                chat_id,
                f"❌ *Generation failed:* _{err}_\n\n"
                f"Job ID   : `{job_id}`\n"
                f"Charge ID: `{ud.get('charge_id', 'N/A')}`\n\n"
                "Share these with support to request a Stars refund.",
                parse_mode="Markdown",
            )
            return

        # ── 3. Download video bytes from FastAPI ─────────────────────────────
        video_bytes = await loop.run_in_executor(None, _api_download, job_id)

        if not video_bytes:
            await bot.send_message(
                chat_id,
                f"❌ Video generated but download failed.\n"
                f"Job ID: `{job_id}` — contact support.",
                parse_mode="Markdown",
            )
            return

        # ── 4. Deliver ───────────────────────────────────────────────────────
        await bot.send_message(chat_id, "✅ *Your video is ready! Uploading…*", parse_mode="Markdown")
        await bot.send_video(
            chat_id,
            video    = BytesIO(video_bytes),
            filename = f"infinitetalk_{job_id}.mp4",
            caption  = (
                f"🎬 *Your InfiniteTalk video!*\n\n"
                f"📐 {RESOLUTIONS[ud['resolution_key']]['label']}\n"
                f"Powered by Wan2.1 · /start to make another"
            ),
            parse_mode="Markdown",
        )

    finally:
        # Clean up temp files regardless of outcome
        for key in ("image_path", "audio_path", "audio_path_raw"):
            p = ud.get(key)
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("image_path", "audio_path", "audio_path_raw"):
        p = ctx.user_data.get(key)
        if p and os.path.exists(p):
            try: os.unlink(p)
            except Exception: pass
    await update.message.reply_text(
        "❌ Cancelled. /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /refund  (admin-only) ─────────────────────────────────────────────────────
# Usage: /refund <user_id> <telegram_payment_charge_id>

async def cmd_refund(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: `/refund <user_id> <charge_id>`", parse_mode="Markdown"
        )
        return
    user_id, charge_id = args
    try:
        await ctx.bot.refund_star_payment(
            user_id=int(user_id),
            telegram_payment_charge_id=charge_id,
        )
        await update.message.reply_text(
            f"✅ Stars refunded for charge `{charge_id}`.", parse_mode="Markdown"
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Refund failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# App assembly
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STATE_IMAGE: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, receive_image),
            ],
            STATE_CHOOSE_AUDIO_METHOD: [
                CallbackQueryHandler(choose_audio_method, pattern="^audio_"),
            ],
            STATE_AUDIO: [
                MessageHandler(
                    filters.VOICE | filters.AUDIO | filters.Document.ALL,
                    receive_audio,
                ),
            ],
            STATE_TTS_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tts_text),
            ],
            STATE_TTS_ENGINE: [
                CallbackQueryHandler(choose_tts_engine, pattern="^tts_engine_"),
            ],
            STATE_TTS_VOICE: [
                CallbackQueryHandler(choose_tts_voice, pattern="^tts_voice_"),
            ],
            STATE_CLONE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tts_text),  # Placeholder
            ],
            STATE_CLONE_AUDIO: [
                MessageHandler(
                    filters.VOICE | filters.AUDIO | filters.Document.ALL,
                    receive_audio,  # Placeholder
                ),
            ],
            STATE_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt),
            ],
            STATE_ORIENTATION: [
                CallbackQueryHandler(choose_orientation, pattern="^orient_"),
            ],
            STATE_RESOLUTION: [
                CallbackQueryHandler(
                    choose_resolution,
                    pattern="^(portrait|landscape|square)_",
                ),
            ],
            STATE_CONFIRM: [
                MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_success),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))   # must be outside conv
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("free",   cmd_free))

    logger.info("InfiniteTalk Telegram Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
