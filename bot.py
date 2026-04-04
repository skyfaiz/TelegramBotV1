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
MAX_AUDIO_SECONDS     = 25

# ── Pricing in Telegram Stars (XTR) ─────────────────────────────────────────
# 1 Star ≈ ₹1.50  (Telegram: 100 Stars ≈ $1.99 ≈ ₹165 at ₹83/$)
# ₹200/min ÷ ₹1.50/Star ≈ 133 Stars/min  → rounded to 130
# ₹120/min → 80 Stars/min
# ₹ 60/min → 40 Stars/min
STARS_PER_MINUTE = {"1080": 130, "720": 80, "480": 40}
TIER_LABEL        = {"1080": "Full HD (1080p)", "720": "HD (720p)", "480": "SD (480p)"}
INR_PER_STAR      = 1.50   # display only; actual rate set by Telegram

# ── Resolutions (Wan2.1-compatible – all multiples of 64) ───────────────────
RESOLUTIONS = {
    "portrait_480":   {"w": 480,  "h": 832,  "tier": "480",  "label": "📱 Portrait  480p  (480×832)"},
    "portrait_720":   {"w": 720,  "h": 1280, "tier": "720",  "label": "📱 Portrait  720p  (720×1280)"},
    "portrait_1080":  {"w": 1080, "h": 1920, "tier": "1080", "label": "📱 Portrait 1080p (1080×1920)"},
    "landscape_480":  {"w": 832,  "h": 480,  "tier": "480",  "label": "🖥 Landscape  480p  (832×480)"},
    "landscape_720":  {"w": 1280, "h": 720,  "tier": "720",  "label": "🖥 Landscape  720p  (1280×720)"},
    "landscape_1080": {"w": 1920, "h": 1080, "tier": "1080", "label": "🖥 Landscape 1080p (1920×1080)"},
}

# ── GPU time estimate: seconds of compute per second of video ────────────────
GENERATION_SPEED = {"480": 6, "720": 10, "1080": 18}

# ── Conversation states ──────────────────────────────────────────────────────
STATE_IMAGE, STATE_AUDIO, STATE_PROMPT, \
STATE_ORIENTATION, STATE_RESOLUTION, STATE_CONFIRM = range(6)

logging.basicConfig(
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════════

def stars_for_job(audio_s: float, tier: str) -> int:
    return max(1, math.ceil(audio_s / 60.0 * STARS_PER_MINUTE[tier]))

def estimate_wait(audio_s: float, tier: str) -> int:
    return math.ceil(audio_s * GENERATION_SPEED[tier])

def fmt_wait(secs: int) -> str:
    if secs < 60:
        return f"~{secs} sec"
    m, s = divmod(secs, 60)
    return f"~{m} min {s} sec" if s else f"~{m} min"

def crop_audio(raw: bytes, fmt: str) -> tuple[bytes, float]:
    """
    Decode audio, trim to MAX_AUDIO_SECONDS if needed,
    re-encode as MP3, return (mp3_bytes, actual_duration_s).
    """
    seg = AudioSegment.from_file(BytesIO(raw), format=fmt)
    dur = len(seg) / 1000.0
    out = seg[: MAX_AUDIO_SECONDS * 1000] if dur > MAX_AUDIO_SECONDS else seg
    buf = BytesIO()
    out.export(buf, format="mp3")
    return buf.getvalue(), min(dur, MAX_AUDIO_SECONDS)


# ═══════════════════════════════════════════════════════════════════════════════
# Keyboard builders
# ═══════════════════════════════════════════════════════════════════════════════

def orientation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 Portrait",  callback_data="orient_portrait"),
        InlineKeyboardButton("🖥 Landscape", callback_data="orient_landscape"),
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


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
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
    await update.message.reply_text(
        "*InfiniteTalk Bot – Help*\n\n"
        "1️⃣ Send a face *photo*\n"
        "2️⃣ Send a *voice/audio* (≤ 25 s, auto-trimmed)\n"
        "3️⃣ Type a *prompt* (speaking style)\n"
        "4️⃣ Choose *orientation* and *resolution*\n"
        "5️⃣ Pay with ⭐ *Telegram Stars*\n"
        "6️⃣ Receive your *video*!\n\n"
        "*Star pricing (per generation):*\n"
        "  SD 480p   →  40 ⭐ / min\n"
        "  HD 720p   →  80 ⭐ / min\n"
        "  Full HD   → 130 ⭐ / min\n\n"
        "1 ⭐ ≈ ₹1.50 (Telegram sets the actual rate)\n\n"
        "/start  – begin a new video\n"
        "/cancel – abort current session",
        parse_mode="Markdown",
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
        "🎵 *Step 2* — Send a *voice message or audio file*.\n"
        "Maximum 25 seconds — longer clips are trimmed automatically.",
        parse_mode="Markdown",
    )
    return STATE_AUDIO


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
        audio_bytes, duration = crop_audio(raw, fmt)
    except Exception as exc:
        logger.error("crop_audio failed: %s", exc)
        await msg.reply_text(
            "❌ Could not read that audio file.\n"
            "Please try MP3, OGG, WAV, or send a voice message."
        )
        return STATE_AUDIO

    ctx.user_data["audio_duration"] = duration
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(audio_bytes); tmp.close()
    ctx.user_data["audio_path"] = tmp.name

    trim_note = "\n_✂️ Trimmed to 25 s._" if duration >= MAX_AUDIO_SECONDS else ""
    await msg.reply_text(
        f"✅ Audio ready — *{duration:.1f} s*{trim_note}\n\n"
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
    dur     = ctx.user_data["audio_duration"]
    stars   = stars_for_job(dur, tier)
    wait_s  = estimate_wait(dur, tier)

    ctx.user_data.update({
        "resolution_key": res_key,
        "width":          res["w"],
        "height":         res["h"],
        "tier":           tier,
        "stars_due":      stars,
        "wait_seconds":   wait_s,
    })

    # Unique payload ties this payment to this session
    payload = f"job_{uuid.uuid4().hex}"
    ctx.user_data["invoice_payload"] = payload

    inr_approx = round(stars * INR_PER_STAR)

    await q.edit_message_text(
        "*Order Summary*\n\n"
        f"🖼 Resolution : {res['label']}\n"
        f"💎 Quality    : {TIER_LABEL[tier]}\n"
        f"🎵 Audio      : {dur:.1f} s\n"
        f"⏱ Est. wait  : {fmt_wait(wait_s)}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Cost       : *{stars} ⭐ Stars*  (~₹{inr_approx})\n\n"
        "Tap *Pay* on the invoice below to start generation! 👇",
        parse_mode="Markdown",
    )

    # Send the native Telegram Stars invoice
    # provider_token="" means XTR (Stars) — no external payment provider needed
    await ctx.bot.send_invoice(
        chat_id     = q.message.chat_id,
        title       = "🎬 InfiniteTalk Video",
        description = f"{TIER_LABEL[tier]} · {res['label']} · {dur:.1f} s audio",
        payload     = payload,
        provider_token = "",          # Empty = Telegram Stars
        currency    = "XTR",
        prices      = [LabeledPrice(label="Video generation", amount=stars)],
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
        for key in ("image_path", "audio_path"):
            p = ud.get(key)
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("image_path", "audio_path"):
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
            STATE_AUDIO: [
                MessageHandler(
                    filters.VOICE | filters.AUDIO | filters.Document.ALL,
                    receive_audio,
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
                    pattern="^(portrait|landscape)_",
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

    logger.info("InfiniteTalk Telegram Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
