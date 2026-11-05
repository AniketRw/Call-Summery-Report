from openai import OpenAI

import re
import subprocess
import os
import time
import json
import sys
import difflib
import hashlib
import itertools

print("IMPORTING testnew.py")
print("PID:", os.getpid())



def load_env_file(env_path=".env"):
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file()

# ---------------------------------------------------------------------------
# ENGINE SPLIT:
#   - Audio transcription (Whisper)              -> Groq
#   - Speaker labeling / analysis / summary (LLM) -> DeepSeek
# Gemini is no longer used anywhere in this file.
# ---------------------------------------------------------------------------


from groq import Groq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")



def get_groq_client():
    if not GROQ_API_KEY:
        raise RuntimeError("Set GROQ_API_KEY in your .env file before running this app.")
    return Groq(api_key=GROQ_API_KEY)


def get_deepseek_client():
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("Set DEEPSEEK_API_KEY in your .env file before running this app.")
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


WHISPER_MODEL = "whisper-large-v3-turbo"   # Groq

# "deepseek-chat" alias was retired by DeepSeek on 24 July 2026.
# Current live models: deepseek-v4-flash (cheap/fast) and deepseek-v4-pro (higher quality).
DEEPSEEK_MODEL = "deepseek-v4-flash"

ANALYSIS_MODEL = DEEPSEEK_MODEL
SUMMARY_MODEL = DEEPSEEK_MODEL
LABELING_MODEL = DEEPSEEK_MODEL

SUMMARY_SEED = 12345

# ---------------------------------------------------------------------------
# DeepSeek V4 Flash official pricing (per 1M tokens, cache-miss input).
# Update these two numbers here if DeepSeek changes rates.
# ---------------------------------------------------------------------------
USD_TO_INR = 95.0
DEEPSEEK_INPUT_PRICE_PER_1M = 0.14
DEEPSEEK_OUTPUT_PRICE_PER_1M = 0.28

def empty_deepseek_usage():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_inr": 0.0
    }


def add_deepseek_usage(total, usage_info):
    if not usage_info:
        return total

    total["input_tokens"] += usage_info.get("input_tokens", 0)
    total["output_tokens"] += usage_info.get("output_tokens", 0)
    total["thinking_tokens"] += usage_info.get("thinking_tokens", 0)
    total["total_tokens"] += usage_info.get("total_tokens", 0)
    total["cost_usd"] += usage_info.get("cost_usd", 0.0)
    total["cost_inr"] += usage_info.get("cost_inr", 0.0)

    return total
    


def calculate_deepseek_cost(usage):
    """Real DeepSeek cost from an OpenAI-style usage object
    (prompt_tokens / completion_tokens / total_tokens)."""
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_inr": 0.0
        }

    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", None) or (input_tokens + output_tokens)

    cost_usd = (
        (input_tokens / 1_000_000) * DEEPSEEK_INPUT_PRICE_PER_1M
        + (output_tokens / 1_000_000) * DEEPSEEK_OUTPUT_PRICE_PER_1M
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": 0,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "cost_inr": cost_usd * USD_TO_INR
    }


TRANSCRIPT_CORRECTIONS = {
    "loss sheet report": "loss sale report",
    "loss seal report": "loss sale report",
    "los sale report": "loss sale report",
    "loss sell report": "loss sale report",
    "lost sale report": "loss sale report",
    "loss cell report": "loss sale report",
}

def get_audio_hash(audio_path):
    sha256 = hashlib.sha256()

    with open(audio_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()

RESULT_CACHE_DIR = "result_cache"

os.makedirs(RESULT_CACHE_DIR, exist_ok=True)

def load_cached_result(audio_path):
    audio_hash = get_audio_hash(audio_path)
    cache_file = os.path.join(
        RESULT_CACHE_DIR,
        audio_hash + ".json"
    )

    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print("Cache read failed:", e, flush=True)
        return None


def save_cached_result(audio_path, result):
    audio_hash = get_audio_hash(audio_path)

    cache_file = os.path.join(
        RESULT_CACHE_DIR,
        audio_hash + ".json"
    )

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(
                result,
                f,
                ensure_ascii=False,
                indent=2
            )

        print("CACHE SAVED:", cache_file, flush=True)

    except Exception as e:
        print("Cache save failed:", e, flush=True)


whisper_prompt = None
# Retailware customer support call.
# The call may contain Marathi, Hindi, English, or Hinglish.

# Common support terms:
# product transfer, product scanning, product scan, barcode,
# system ID, password, report, billing, purchase, sales, stock,
# sync, printer, database, branch, scheme, loss sale report.

# Transcribe only what is actually spoken.
# Do not invent names, products, companies, IDs, numbers, or technical terms.
# """

def whisper_transcribe(audio_path):
    print(">>> ENTERED GROQ WHISPER TRANSCRIPTION", flush=True)
    print("Audio exists:", os.path.exists(audio_path), flush=True)
    print("Audio size:", os.path.getsize(audio_path), "bytes", flush=True)
    print("Audio path:", audio_path, flush=True)

    client = get_groq_client()
    max_retries = 4
    response = None
    for attempt in range(max_retries):
        try:
            with open(audio_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), f.read()),
                    model=WHISPER_MODEL,
                    prompt=whisper_prompt,
                    response_format="verbose_json",
                    temperature=0.0
                )
            break
        except Exception as e:
            print(f"[Groq] Retry {attempt+1}/{max_retries}: {e}", flush=True)
            if attempt == max_retries - 1:
                raise
            time.sleep(3 * (attempt + 1))

    segments = getattr(response, "segments", None) or []
    print("WHISPER LANGUAGE: mr", flush=True)
    print("GROQ SEGMENT COUNT:", len(segments), flush=True)

    groq_total = 0
    for seg in segments:
        if isinstance(seg, dict):
            seg_tokens = seg.get("tokens", []) or []
        else:
            seg_tokens = getattr(seg, "tokens", []) or []

        groq_total += len(seg_tokens)

    print("GROQ WHISPER TOKENS:", groq_total, flush=True)

    lines = []
    for seg in segments:
        start = seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0)
        text = seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")
        text = (text or "").strip()
        if not text:
            continue
        m = int(start // 60)
        s = int(start % 60)
        print("GROQ SEGMENT:", start, repr(text), flush=True)
        lines.append(f"[{m:02d}:{s:02d}] {text}")

    if not lines:
        fallback_text = (getattr(response, "text", "") or "").strip()
        if fallback_text:
            lines.append(f"[00:00] {fallback_text}")

    return "\n".join(lines), groq_total
    
def is_degenerate_text(text, min_char_run=25, min_word_repeats=35):
    if not text:
        return False
    if re.search(r'(.)\1{' + str(min_char_run - 1) + r',}', text, re.DOTALL):
        return True
    if re.search(r'(\S+)(?:\s+\1){' + str(min_word_repeats - 1) + r',}', text):
        return True
    return False


def clean_degenerate_output(raw_text, min_char_run=25, min_word_repeats=35):
    match = re.search(r'(.)\1{' + str(min_char_run - 1) + r',}', raw_text, re.DOTALL)
    if not match:
        match = re.search(r'(\S+)(?:\s+\1){' + str(min_word_repeats - 1) + r',}', raw_text)
    if match:
        before = raw_text[:match.start()].rstrip()
        after = raw_text[match.end():].lstrip()
        raw_text = (
            before
            + "\n[... repeated/unclear audio segment omitted ...]\n"
            + after
        )
    return raw_text


def apply_corrections(text):
    for wrong, right in TRANSCRIPT_CORRECTIONS.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bloss\s+(?!sale\b)\w+\s+report\b",
        "loss sale report",
        text,
        flags=re.IGNORECASE
    )
    return text


# Standard name -> all spelling/script variants that might appear in transcript
KNOWN_EXECUTIVE_NAMES = {
    "Ajit Sir": ["ajit", "ajeet", "अजीत", "अजित"],
    "Rahul": ["rahul", "राहुल"],
    "Sandeep": ["sandeep", "sandip", "संदीप"],
    "Vidya": ["vidya", "विद्या"],
    "Akshay": ["akshay", "अक्षय"],
    "Suresh": ["suresh", "सुरेश"],
    "Avinash": ["avinash", "अविनाश"],
    "Amol": ["amol", "अमोल"],
    "Mahendra": ["mahendra", "महेंद्र"],
    "Dilip": ["dilip", "deelip", "दिलीप"],
    "Dipak": ["dipak", "deepak", "दीपक"],
    "Gautam": ["gautam", "गौतम"],
    "Sagar": ["sagar", "सागर"],
    "Sandeep Gosavi": ["sandeep gosavi", "गोसावी संदीप"],
    "Rajesh": ["rajesh", "राजेश "],
    "Akash": ["akash", "आकाश "],
    "Kishor": ["kishor", "किशोर"],
    "Prathmesh": ["prathmesh", "प्रथमेश"],
    "Pratik": ["pratik", "प्रतीक"],
    "Abhijit": ["abhijit", "अभिजीत"],
    "Aniket": ["aniket", "अनिकेत"],
}


def _build_alias_lookup(name_dict):
    lookup = {}
    for standard, variants in name_dict.items():
        for v in variants:
            lookup[v.strip().lower()] = standard
    return lookup


KNOWN_EXECUTIVE_ALIAS_LOOKUP = _build_alias_lookup(KNOWN_EXECUTIVE_NAMES)

STOPWORDS_NOT_NAMES = {
    "मी", "आम्ही", "हा", "ही", "ते", "सर", "मॅडम",
    "sir", "madam", "i", "we", "you", "he", "she",
    "कडनं", "कडून", "कडुन", "वेअर", "रिटेलवेअर", "रिटेल",
    "retailware", "कंपनी", "team", "टीम", "support",
    "सपोर्ट", "हेल्पडेस्क", "helpdesk"
}


def normalize_executive_name(raw_name):
    """Dictionary मध्ये match झालं तर standard spelling वापर,
    नाहीतर raw name ला title-case करून तसंच वापर (gate नाही)."""
    key = (raw_name or "").strip().lower()
    if not key or key in STOPWORDS_NOT_NAMES:
        return None
    if key in KNOWN_EXECUTIVE_ALIAS_LOOKUP:
        return KNOWN_EXECUTIVE_ALIAS_LOOKUP[key]
    if re.match(r'^[a-zA-Z]+$', raw_name.strip()):
        return raw_name.strip().title()
    return raw_name.strip()

def safe_generate(client, model, messages, temperature=0, max_tokens=6000, json_mode=False):
    """OpenAI-compatible chat-completion wrapper with retry."""

    max_retries = 6

    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # DeepSeek V4 Flash:
    # Disable thinking/reasoning for short deterministic tasks.
    if model == DEEPSEEK_MODEL:
        kwargs["extra_body"] = {
            "thinking": {
                "type": "disabled"
            }
        }

    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)

        except Exception as e:
            err = str(e).lower()
            print(f"Retry {attempt+1}/{max_retries}: {e}")

            retryable = any(x in err for x in [
                "server disconnected",
                "timeout",
                "503",
                "connection",
                "429",
                "internal",
                "unavailable",
                "rate limit",
                "rate_limit"
            ])

            if not retryable:
                raise

            if attempt == max_retries - 1:
                raise

            wait = 3 * (attempt + 1)
            print(f"Waiting {wait}s before retry...")
            time.sleep(wait)


def label_speakers_with_deepseek(client, plain_transcript):
    """Groq Whisper कडून आलेल्या [MM:SS] text (speaker label नसलेल्या) transcript ला
    DeepSeek कडून Executive/Customer label लावून घेतो — text-only, fast."""
    if not plain_transcript.strip():
        return plain_transcript, empty_deepseek_usage()

    prompt = (
        "You are given a support call transcript where each line has a "
        "[MM:SS] timestamp but NO speaker label. Add a speaker label "
        "('Executive' or 'Customer') to the START of every line:\n"
        "[MM:SS] Executive: <text>\n[MM:SS] Customer: <text>\n\n"
        "RULES:\n"
        "- Executive = Retailware support agent (usually opens with a "
        "self-introduction mentioning Retailware / रिटेलवेअर).\n"
        "- Customer = person reporting the issue.\n"
        "- Use context: who asks for ID/password/troubleshoots (Executive), "
        "who reports the problem and answers (Customer).\n"
        "- Keep the SAME speaker consistent throughout unless there's clear "
        "evidence of a turn change.\n"
        "- If a line is a side remark to someone nearby (unrelated to the "
        "support topic), add [Aside] after the label.\n"
        "- Do NOT change, merge, reorder, or drop any line's text — only add labels.\n"
        "- Keep original language/script exactly as given.\n\n"
        "Transcript:\n" + plain_transcript + "\n\n"
        "Output ONLY the relabeled transcript, nothing else."
    )

    response = safe_generate(
        client,
        model=LABELING_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=1200,
    )
    labeled = (response.choices[0].message.content or "").strip()

    print("========== DEEPSEEK LABELED OUTPUT ==========")
    print(labeled)
    print("==============================================")

    return (
        labeled if labeled else plain_transcript,
        calculate_deepseek_cost(response.usage)
    )


def fix_speaker_consistency(transcript: str) -> str:
    lines = transcript.splitlines()
    fixed = []

    prev_text = ""

    for line in lines:
        m = re.match(r"(\[\s*\d{2}:\d{2}\s*\])\s*(Executive|Customer)(\s*\[[^\]]+\])?\s*:\s*(.*)", line)

        if not m:
            fixed.append(line)
            continue

        ts = m.group(1)
        speaker = m.group(2)
        aside = m.group(3) or ""
        text = m.group(4).strip()

        lower = text.lower()

        if "आपका नाम" in lower or "नाम?" in lower:
            speaker = "Customer"

        elif prev_text.startswith("आपका नाम"):
            if (
                len(text.split()) <= 3
                and "लिखो" not in lower
                and "लिखिए" not in lower
                and "सामने" not in lower
            ):
                speaker = "Executive"
            elif (
                "लिखो" in lower
                or "लिखिए" in lower
                or "सामने" in lower
            ):
                speaker = "Customer"

        elif "पासवर्ड" in lower:
            speaker = "Executive"

        elif "पासवर्ड" in prev_text and len(text.split()) <= 5:
            speaker = "Customer"

        elif "आईडी" in lower or "id" in lower:
            speaker = "Executive"

        elif ("आईडी" in prev_text or "id" in prev_text) and len(text.split()) <= 5:
            speaker = "Customer"

        elif "कंप्लेंट नंबर" in lower or "complaint number" in lower:
            speaker = "Executive"

        elif "कंप्लेंट नंबर" in prev_text and len(text.split()) <= 5:
            speaker = "Customer"

        fixed.append(f"{ts} {speaker}{aside}: {text}")
        prev_text = text

    return "\n".join(fixed)


def extract_speaker_text(transcript, speaker="Customer"):
    """Extract ONLY the given speaker's actual dialogue (skipping [Aside]
    lines), so mood/tone keyword checks don't get polluted."""
    lines = transcript.splitlines()
    out = []
    pattern = re.compile(
        rf"\[\s*\d{{2}}:\d{{2}}\s*\]\s*{speaker}\s*(\[[^\]]*\])?\s*:\s*(.*)"
    )
    for line in lines:
        m = pattern.match(line.strip())
        if m:
            aside = (m.group(1) or "").lower()
            if "aside" in aside:
                continue
            out.append(m.group(2))
    return " ".join(out).lower()


def detect_known_executive(transcript):
    transcript_lower = transcript.lower()

    self_intro_patterns = [
        r"([a-zA-Z\u0900-\u097F]+)\s*बोलतोय",
        r"([a-zA-Z\u0900-\u097F]+)\s*बोल रहा",
        r"this is\s+([a-zA-Z]+)\s+speaking",
        r"मेरा नाम\s+([a-zA-Z\u0900-\u097F]+)",
        r"माझं नाव\s+([a-zA-Z\u0900-\u097F]+)",
        r"(?:रिटेलवेअर|retailware)[^,।\n]{0,25}?(?:कडून|कडनं)[^,।\n]{0,25}?"
        r"बोलतोय[^,।\n]{0,10}?(?:सर|मॅडम)?\s*,?\s*([a-zA-Z\u0900-\u097F]+)",
    ]
    for pat in self_intro_patterns:
        m = re.search(pat, transcript, flags=re.IGNORECASE)
        if m:
            normalized = normalize_executive_name(m.group(1))
            if normalized:
                return normalized

    for standard_name, variants in KNOWN_EXECUTIVE_NAMES.items():
        for variant in variants:
            if re.search(r'\b' + re.escape(variant.lower()) + r'\b', transcript_lower):
                return standard_name
    return None


def find_speech_start(wav_path):
    """Detect where ring-back tone ends and speech begins."""
    import wave
    import struct
    try:
        import numpy as np
    except ImportError:
        return 0.0

    tmp_wav = wav_path + "_probe.wav"
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", wav_path,
            "-t", "15",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            tmp_wav
        ], capture_output=True, text=True)

        if r.returncode != 0 or not os.path.exists(tmp_wav):
            print(f"[Ring trim] Probe conversion failed: {r.stderr[:200]}")
            return 0.0

        with wave.open(tmp_wav, 'rb') as w:
            framerate = w.getframerate()
            max_frames = int(framerate * 15)
            data = w.readframes(min(w.getnframes(), max_frames))

        samples = np.array(struct.unpack('<' + 'h' * (len(data) // 2), data), dtype=np.float32)
        step = int(framerate * 0.5)
        ring_pcts = []
        for i in range(0, len(samples) - step, step):
            seg = samples[i:i + step]
            rms = float(np.sqrt(np.mean(seg ** 2)))
            if rms < 200:
                ring_pcts.append(None)
                continue
            fft = np.abs(np.fft.rfft(seg))
            freqs = np.fft.rfftfreq(step, 1.0 / framerate)
            ring_mask = (freqs >= 300) & (freqs <= 500)
            speech_mask = (freqs >= 100) & (freqs <= 3400) & ~ring_mask
            ring_e = float(np.sum(fft[ring_mask]))
            speech_e = float(np.sum(fft[speech_mask]))
            ring_pcts.append(ring_e / max(ring_e + speech_e, 1))

        consecutive = 0
        for idx, rp in enumerate(ring_pcts):
            t = idx * 0.5
            if rp is None:
                if consecutive > 0:
                    consecutive += 1
                continue
            if rp < 0.40:
                consecutive += 1
                if consecutive >= 3:
                    speech_start = max(0.0, t - 1.0)
                    print(f"[Ring trim] Speech at {t:.1f}s → trim from {speech_start:.1f}s")
                    return speech_start
            else:
                consecutive = 0

    except Exception as e:
        print(f"[Ring trim] Detection failed: {e}, using full audio")
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    return 0.0


def trim_internal_silences(input_path, silence_threshold_db=-35, min_silence_duration=1.5):
    """Removes long silent gaps (hold music, dead air, line noise) from
    ANYWHERE in the audio — not just the start. This prevents Whisper from
    hallucinating repeated/garbage text during silent stretches (e.g. calls
    on hold for 60-90+ seconds).

    silence_threshold_db: audio below this level is considered silence
    min_silence_duration: only gaps LONGER than this (seconds) get removed,
                           so brief natural pauses in speech are untouched
    """
    start_time = time.time()
    output_path = os.path.splitext(input_path)[0] + "_desilenced.wav"

    # stop_periods=-1 => apply repeatedly through the whole file, not just once
    silence_filter = (
        f"silenceremove="
        f"stop_periods=-1:"
        f"stop_duration={min_silence_duration}:"
        f"stop_threshold={silence_threshold_db}dB:"
        f"start_periods=0"  # don't touch leading silence here — ring-trim handles that separately
    )

    result = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-af", silence_filter,
        "-ac", "1", "-ar", "8000",
        output_path
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[Silence trim] FFmpeg failed, using original audio: {result.stderr.strip()[:300]}")
        return input_path

    original_size = os.path.getsize(input_path)
    new_size = os.path.getsize(output_path)
    print(f"[Silence trim] {original_size} -> {new_size} bytes "
          f"({time.time() - start_time:.2f}s)")

    return output_path


def convert_audio(input_path):
    """Convert audio to 16 kHz mono, trimming ring-back tone AND long
    internal silence gaps (hold periods, dead air) that cause Whisper
    to hallucinate repeated/garbage text."""
    start_time = time.time()

    # STEP 1: Remove long silent gaps ANYWHERE in the original audio first
    # (working on the original file, before any lossy re-encoding)
    #desilenced_path = trim_internal_silences(input_path)
    desilenced_path = input_path
    speech_start = find_speech_start(input_path)

    print(f"[DEBUG] Detected speech start: {speech_start:.2f} seconds")
    #output_path = os.path.splitext(input_path)[0] + "_16k.ogg"
    output_path = os.path.splitext(input_path)[0] + "_16k.wav"
    result = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        
        "-i", desilenced_path,
        "-vn", "-ac", "1", "-ar", "16000",
        # "-af", "highpass=f=250,lowpass=f=3800",
        # "-c:a", "libopus", "-b:a", "96k",
        "-c:a", "pcm_s16le",
        output_path
    ], capture_output=True, text=True)

    # Clean up intermediate desilenced file if it's not the original
    if desilenced_path != input_path and os.path.exists(desilenced_path):
        try:
            os.remove(desilenced_path)
        except Exception:
            pass

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[:300]}")

    # STEP 2: Existing ring-back tone trim (for the very start of the call)
    # speech_start = find_speech_start(output_path)
    # if speech_start > 2.0:
    #     trimmed = os.path.splitext(input_path)[0] + "_trimmed.wav"
    #     r2 = subprocess.run([
    #         "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    #         "-i", output_path,
    #         "-ss", str(speech_start),
    #         "-c", "copy", trimmed
    #     ], capture_output=True, text=True)
    #     if r2.returncode == 0:
    #         os.replace(trimmed, output_path)
    #         print(f"[Ring trim] Removed {speech_start:.1f}s of ring tone")
    #     else:
    #         print("[Ring trim] Trim step failed, using full audio")

    elapsed = time.time() - start_time
    print(f"[TIMER] convert_audio took {elapsed:.2f} seconds")
    return output_path


def parse_llm_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


# Backwards-compatible alias (old name used elsewhere)
parse_gemini_json = parse_llm_json


def clean_json_string(raw_text):
    text = raw_text.strip()

    def escape_inside_quotes(match):
        return match.group(0).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    return re.sub(r'"(?:\\.|[^"\\])*"', escape_inside_quotes, text)


def format_facts(facts):
    if not isinstance(facts, dict):
        return str(facts or "").strip()
    labels = [("issues", "Customer issues"), ("requests", "Customer requests"), ("actions", "Support action")]
    sections = []
    for key, label in labels:
        vals = facts.get(key) or ["None detected"]
        sections.append(f"{label}:\n" + "\n".join(f"- {v}" for v in (vals if isinstance(vals, list) else [vals])))
    return "\n".join(sections)


def collapse_alternating_filler(transcript, filler_words=None, min_run=4):
    if filler_words is None:
        filler_words = {"हां", "हं", "हम्म", "ओके", "ok", "okay"}
    lines = transcript.split("\n")
    result, buffer = [], []

    def flush():
        if len(buffer) >= min_run:
            ts = re.search(r"\[\s*\d{2}:\d{2}\s*\]", buffer[0])
            result.append(f"{ts.group(0) if ts else ''} [... unclear/filler exchange omitted ...]")
        else:
            result.extend(buffer)
        buffer.clear()

    for line in lines:
        m = re.match(r"(\[\s*\d{2}:\d{2}\s*\])\s*(?:Executive|Customer)\s*(?:\[[^\]]*\]\s*)?:\s*(.*)", line)
        content = m.group(2).strip().rstrip("।.").strip() if m else None
        if content and content in filler_words:
            buffer.append(line)
        else:
            flush()
            result.append(line)
    flush()
    return "\n".join(result)


def merge_orphan_lines(transcript):
    """Any line that doesn't start with [MM:SS] gets merged into the
    previous line — fixes stray mid-line newlines."""
    lines = transcript.split("\n")
    merged = []
    for line in lines:
        if re.match(r"^\[\s*\d{2}:\d{2}\s*\]", line) or not merged:
            merged.append(line)
        else:
            merged[-1] = merged[-1].rstrip() + " " + line.strip()
    return "\n".join(merged)


def collapse_repeated_lines(transcript, max_repeats=3):
    lines = transcript.split("\n")
    result = []
    last_content = None
    repeat_count = 0

    for line in lines:
        m = re.match(r"(\[\s*\d{2}:\d{2}\s*\])\s*(.*)", line)
        if not m:
            result.append(line)
            continue
        timestamp, content = m.groups()
        content_key = content.strip()

        if content_key and content_key == last_content:
            repeat_count += 1
            if repeat_count < max_repeats:
                result.append(line)
            elif repeat_count == max_repeats:
                result.append(f"{timestamp} [... repeated content omitted ...]")
        else:
            repeat_count = 0
            last_content = content_key
            result.append(line)

    return "\n".join(result)


def collapse_alternating_pairs(transcript, min_cycles=6, max_period=4):
    """Detects short repeating cycles (A,B,A,B,...) and collapses them."""
    lines = transcript.split("\n")

    def get_content(line):
        m = re.match(r"\[\s*\d{2}:\d{2}\s*\]\s*(.*)", line)
        return m.group(1).strip() if m else None

    contents = [get_content(l) for l in lines]
    result = []
    i = 0
    n = len(lines)

    while i < n:
        collapsed = False
        for period in range(1, max_period + 1):
            if contents[i] is None:
                continue
            cycle = contents[i:i + period]
            if any(c is None for c in cycle):
                continue
            repeats = 1
            j = i + period
            while j + period <= n and contents[j:j + period] == cycle:
                repeats += 1
                j += period
            if repeats >= min_cycles:
                ts_match = re.search(r"\[\s*\d{2}:\d{2}\s*\]", lines[i])
                ts = ts_match.group(0) if ts_match else ""
                result.append(f"{ts} [... repeated/unclear audio segment omitted ...]")
                i = j
                collapsed = True
                break
        if not collapsed:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def load_keywords(file_path="keywords.txt"):
    if not os.path.exists(file_path):
        return [
            "Whatsapp", "PhonePe", "UPI", "Mobile Reports", "GSTR2A",
            "Multibranch", "Dashboard", "Analytics", "Purchase Order",
            "Purchase", "Label designer",
            "Bill designer", "User rights", "Counterwise report",
            "Release notes", "Price Protection", "IMEI tracking",
            "Bill Print", "Expiry tracking", "Interbranch IBT",
            "Sync Issue", "Reports issue",
            "Product Scan Issue", "Bill Modification or edit", "Incentive", "loss",
            "scheme"
        ]
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def generate_detailed_summary(client, clean_transcript, business_facts, executive_name, customer_mood, mood_reason):
    summary_cost_info = calculate_deepseek_cost(None)

    """Second, focused DeepSeek pass: generate ONLY the short + long summary,
    using the transcript as the primary source and business facts as supporting context."""

    facts_str = format_facts(business_facts)

    prompt = f"""
You are summarizing a Retailware customer support call.

The TRANSCRIPT is the PRIMARY SOURCE OF TRUTH.
The business_facts are supporting information extracted from the same transcript.
Do NOT blindly trust business_facts if they are empty, incomplete, or inconsistent with
what is clearly present in the transcript.

Transcript:
{clean_transcript}

Supporting information:
- Executive: {executive_name}
- Customer mood: {customer_mood} ({mood_reason})

Business facts:
{facts_str}

Your task is to understand the ACTUAL conversation and write an accurate summary.

Before writing the summary, identify from the transcript:
1. Why did the customer contact support?
2. What exact issue/problem/request was discussed?
3. What did the Executive do or explain?
4. Was any troubleshooting performed?
5. Was the issue resolved, partially resolved, unresolved, or is follow-up pending?
6. What was the main business topic/feature/report involved?

IMPORTANT TRANSCRIPT INTERPRETATION RULES:
- The transcript may contain Marathi, Hindi, English, Hinglish, or Marathi/Hindi spoken in Roman script.
- Whisper may produce phonetic or imperfect spellings.
- Do NOT assume that imperfect spelling means there was no issue.
- Use context only to understand sentences that are explicitly
  present in the transcript.
- Context must NOT be used to invent missing words or facts.
- Do not normalize an unclear word into a specific business
  term unless that term is clearly supported by the transcript.
- Do NOT invent information that is not supported by the transcript.
- If the transcript contains a clear issue/request/action, include it even if business_facts missed it.
- If the transcript genuinely contains only a greeting and no meaningful support discussion, then say that clearly.
- Do NOT convert a meaningful support conversation into "no issue" merely because the business_facts are empty.
- Do NOT say "no specific issue" unless you have actually checked the complete transcript and there is genuinely no identifiable issue or request.

SPEAKER RULES:
- Executive = Retailware support person.
- Customer = person contacting support/reporting the issue.
- Executive performs troubleshooting, explains solutions, gives instructions, or confirms status.
- Customer reports problems, asks questions, or requests help.
- Never reverse these roles.

SUMMARY REQUIREMENTS:

1. short_summary:
Write 2-3 concise sentences.
Must include:
- why the customer called,
- main issue/request/topic,
- current status/outcome if known.

2. summary:
Write 4-6 factual sentences.
Must include:
- customer's issue/request,
- relevant report/feature/module/topic,
- what the Executive did/explained,
- troubleshooting or guidance provided,
- resolution/current status,
- pending follow-up if any.

STRICT SUMMARY RULES:

- Use ONLY facts explicitly supported by the transcript.
- Do NOT add information from your general knowledge.
- Do NOT infer missing words.
- Do NOT invent IDs, names, products, companies, branches,
  payment methods, reports, or technical details.
- Do NOT treat business_facts as additional facts.
- business_facts may only confirm information already present
  in the transcript.
- If the transcript does not clearly mention a fact, omit it.
- If the resolution is not explicitly confirmed, say:
  "No confirmed resolution was stated in the call."
- Never create a plausible story from incomplete conversation.
- Do NOT fill gaps in the conversation using assumptions.
- Do NOT convert unclear speech into a specific product,
  report, ID, branch, or technical issue.
- If a word or phrase is unclear in the transcript, omit it
  instead of guessing.
- Preserve the actual issue exactly as supported by the transcript.
- If the customer clearly says that a product transfer is not
  working, report that as the issue. Do NOT replace it with
  another product, branch transfer, ID, or technical explanation.


Return valid JSON only, in this exact format:
{{"short_summary": "...", "summary": "..."}}
"""

    response = safe_generate(
        client,
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=2000,
        json_mode=True,
    )

    try:
        summary_usage = response.usage
        summary_cost_info = calculate_deepseek_cost(summary_usage)

        print("\n" + "=" * 60)
        print("DEEPSEEK DETAILED SUMMARY COST")
        print("=" * 60)
        print("Input Tokens    :", summary_cost_info["input_tokens"])
        print("Output Tokens   :", summary_cost_info["output_tokens"])
        print("Total Tokens    :", summary_cost_info["total_tokens"])
        print("Cost USD        : $%.6f" % summary_cost_info["cost_usd"])
        print("Cost INR        : ₹%.4f" % summary_cost_info["cost_inr"])
        print("=" * 60)

    except Exception as e:
        print("Summary token usage not available:", e)

    raw = (response.choices[0].message.content or "").strip()

    try:
        data = parse_llm_json(raw)
    except Exception:
        data = {}

    short_summary = data.get(
        "short_summary",
        "Short summary not generated."
    )

    long_summary = data.get(
        "summary",
        "Summary not generated."
    )

    return (
        short_summary,
        long_summary,
        summary_cost_info
    )
def analyze_audio_with_gemini(audio_path):
    """Name kept as-is for backwards compatibility with web_app.py
    (testnew.analyze_audio_with_gemini(...)). Internally: Groq for
    Whisper transcription, DeepSeek for labeling / analysis / summary."""
    print(">>> ENTERED analyze_audio_with_gemini", flush=True)
    print("====================================")
    print("START analyze_audio_with_gemini")
    print("Audio Path :", audio_path)
    print("====================================")
    cached_result = load_cached_result(audio_path)

    if cached_result:
        print("====================================")
        print("CACHE HIT - RETURNING SAVED RESULT")
        print("====================================", flush=True)

        return (
            cached_result["transcript"],
            cached_result["raw_output"],
            cached_result["business_facts"],
            cached_result["analysis_text"],
            cached_result["combined_summary"],
            cached_result["keywords"],
            cached_result["token_info"]
        )

    print("CACHE MISS - PROCESSING AUDIO", flush=True)
    total_start = time.time()
    deepseek_usage_total = empty_deepseek_usage()
    deepseek_client = get_deepseek_client()  # labeling / analysis / summary
    keywords_list = load_keywords()
    keywords_str = ", ".join(keywords_list)
    try:
        print(">>> STARTING GROQ WHISPER", flush=True)

        transcript, groq_total = whisper_transcribe(audio_path)
        print("\n" + "=" * 80)
        print("DEBUG 1 - RAW WHISPER TRANSCRIPT")
        print("=" * 80)
        print(transcript)
        print("=" * 80)

        transcript = merge_orphan_lines(transcript)
        print("\n" + "=" * 80)
        print("DEBUG 2 - AFTER merge_orphan_lines")
        print("=" * 80)
        print(transcript)
        print("=" * 80)

        if is_degenerate_text(transcript):
            print(">>> DEGENERATE TEXT DETECTED — cleaning up", flush=True)
            transcript = clean_degenerate_output(transcript)

        transcript = collapse_repeated_lines(transcript)
        transcript = collapse_alternating_pairs(transcript)

        transcript = apply_corrections(transcript)
        print("\n" + "=" * 80)
        print("DEBUG 3 - AFTER apply_corrections")
        print("=" * 80)
        print(transcript)
        print("=" * 80)

        transcript, labeling_usage = label_speakers_with_deepseek(
            deepseek_client,
            transcript
        )
        print("\n" + "=" * 80)
        print("DEBUG 4 - AFTER SPEAKER LABELING")
        print("=" * 80)
        print(transcript)
        print("=" * 80)

        deepseek_usage_total = add_deepseek_usage(
            deepseek_usage_total,
            labeling_usage
        )
        transcript = fix_speaker_consistency(transcript)
        transcript = collapse_alternating_filler(transcript)
        print("========== TRANSCRIPT ==========", flush=True)
        print(transcript, flush=True)
        print("=================================", flush=True)
        print("TRANSCRIPT LENGTH:", len(transcript), flush=True)
        print(">>> TRANSCRIPTION + LABELING COMPLETED", flush=True)

        if not transcript:
            empty_token_info = {
                "groq_total": groq_total,
                "prompt": deepseek_usage_total["input_tokens"],
                "response": deepseek_usage_total["output_tokens"],
                "thinking": deepseek_usage_total["thinking_tokens"],
                "total": deepseek_usage_total["total_tokens"],
                "deepseek_input": deepseek_usage_total["input_tokens"],
                "deepseek_output": deepseek_usage_total["output_tokens"],
                "deepseek_thinking": deepseek_usage_total["thinking_tokens"],
                "deepseek_total": deepseek_usage_total["total_tokens"],
                "deepseek_cost_usd": deepseek_usage_total["cost_usd"],
                "deepseek_cost_inr": deepseek_usage_total["cost_inr"]
            }
            return (
                "No transcript generated.",
                "",
                "No facts detected.",
                "No analysis available.",
                "Summary not generated.",
                "None detected",
                empty_token_info
            )
        clean_transcript = transcript
        prompt = f"""Analyze this customer support transcript.

Transcript:
{clean_transcript}

Rules:
- Executive = support person
- Customer = person reporting issue
- Customer usually reports issue and shares system ID/password.
- Executive usually asks for system ID/password, gives troubleshooting, callback timing, and technical guidance.
- Never reverse customer and executive actions.
- IGNORE lines tagged [Aside] (e.g. "Customer [Aside]: ..."), and ignore any other lines that are clearly a
  speaker talking to someone physically nearby rather than to the other person on the call (e.g. asking for
  coins/change, giving an unrelated instruction to shop staff, side remarks unrelated to the support issue).
  Do NOT use these lines for business_facts, greetings, closings, customer_mood, executive_tone, or keywords —
  treat them as if they were not part of the support conversation at all.

Field rules:

1. executive_name:
- Scan the ENTIRE conversation from start to end carefully — the name can appear anywhere: beginning, middle, or end of the call.
- Case A: The customer (or anyone else) addresses the Executive by name at any point — in any language or script (Hindi/Marathi/English, Devanagari or Latin), with or without honorifics like "जी"/"सर"/"ji"/"sir".
  Example: "Thank you Rahul", "Ravi, can you help?", "अजीत जी हेलो", "धन्यवाद अजीत सर", Sandeep
- Case B: The Executive states their own name at any point in the call (not just the beginning) — e.g. "मेरा नाम अजीत है", "This is Rahul speaking", "अजीत बोल रहा हूं".
- Casual, broken, or informal sentence phrasing is fine in both cases — still extract the name as long as it is clearly being used as a name.
- If a name is found under EITHER Case A or Case B anywhere in the transcript, use it as executive_name.
- Only return "Not mentioned" if NO name is spoken anywhere in the entire conversation, by either party.
- NEVER invent a name that is not explicitly present in the transcript.

2. greetings:
- Detect ONLY from lines with timestamp [00:00] to [00:15].
- Ignore greetings appearing after [00:15].
- If no clear greeting phrase is found in that window, return the first spoken line (verbatim, any language) as the greeting — never return an empty array.

3. closings:
- Detect ONLY from the last 15 seconds of the call.
- Ignore mid-call conversation.
- If no clear closing phrase is found in that window, return the last spoken line (verbatim, any language) as the closing — never return an empty array.

4. customer_mood:
- "Frustrated": customer is SHOUTING, raising voice [Loudly]/[Shouting], sounds visibly annoyed/irritated,
  or uses abusive/swear words. This is ONLY about TONE, not topic.
- "Concerned": customer calmly/normally mentions that an issue is repeating, has happened before,
  or that a feature/option is missing — but WITHOUT raising voice, anger, or abusive language.
- "Neutral": calm tone throughout, normal question-answer flow, no recurring issue mentioned.
- "Satisfied": thanks executive, confirms issue resolved, positive closing.
- DEFAULT to "Neutral" if no clear signal exists.
- CRITICAL: Do NOT choose Frustrated just because the customer mentions a problem/issue/complaint — only choose Frustrated if the TONE itself shows it.

5. mood_reason: explain why.

6. executive_tone: one of Helpful / Helpful but Defensive / Defensive / Neutral.

7. keywords:
- Select a keyword ONLY if that specific topic/feature was EXPLICITLY and CLEARLY discussed — the term itself, or a direct/obvious translation of it (Hindi/Marathi), must actually be spoken.
- Do NOT select based on loose thematic association.
- When in doubt, DO NOT include it.
- If nothing clearly discussed, return [].

8. business_facts: issues / requests / actions — split correctly (Customer requests vs Executive actions).

IMPORTANT TRANSCRIPT INTERPRETATION RULE:
- The transcript may contain Marathi, Hindi, English, Hinglish, or Marathi/Hindi spoken using English letters (Roman script).
- Whisper may produce imperfect spelling, grammar, or phonetic transliteration.
- Do NOT treat a sentence as unintelligible only because its spelling or grammar is imperfect.
- If the transcript says "product scanning", "product scan", "product barcode scanning", or an equivalent clearly identifiable phrase, the issue MUST retain the word "product".
- Example: "Product scanning is not working" -> "Product scanning not working".
- Do NOT reduce "Product scanning not working" to "Scanning not working".
- Preserve specific report names, module names, feature names, product names, error terms, and technical terms whenever they are reasonably identifiable from the transcript.
- When a specific business term is slightly misspelled by Whisper but the intended term is clear from context, normalize it to the correct business term rather than removing the specificity.
- If the transcript clearly discusses product scanning / product scan / scanning a product barcode and the issue is that it is not working, select "Product Scan Issue".
- If the transcript clearly indicates a business issue, request, action, or outcome despite imperfect transliteration, capture it in business_facts.
- If the transcript clearly indicates an Executive action, put it in business_facts.actions.
- If the transcript clearly indicates a Customer request or requirement, put it in business_facts.requests.
- If the transcript clearly indicates a problem/issue, put it in business_facts.issues.
- Do NOT invent an issue, request, product name, technical detail, or resolution that is not supported by the transcript.
- Do NOT mark a call as unintelligible only because the Marathi/Hindi/Hinglish is imperfectly transcribed.
- Example: "upload ke le le report ... download karun ... solve ke le" should be interpreted using the surrounding context. If the context supports it, capture the report upload/download action and resolution-related outcome in business_facts.actions.

IMPORTANT ANTI-HALLUCINATION RULES:

- The transcript is the ONLY source of truth.
- NEVER invent or assume information.
- Do not infer a product name, company name, payment method,
  branch name, ID, password, customer name, executive name,
  report name, or technical term unless it is explicitly supported
  by the transcript.
- Do not convert unclear speech into a specific known brand or product.
- If a word or phrase is unclear, omit it rather than guessing.
- Every keyword MUST be supported by the transcript.
- Every customer issue MUST be supported by the transcript.
- Every customer request MUST be supported by the transcript.
- Every executive action MUST be supported by the transcript.
- If something is not clearly supported, return an empty list or
  "Not mentioned".
- Accuracy is more important than completeness.


9. other_topics:
- Independently identify the MAIN issue/topic/feature discussed in this call, in 1-4 short phrases, regardless of whether it matches the Keywords list.

Keywords list to check against:
{keywords_str}

Return ONLY valid JSON (no markdown, no commentary) matching EXACTLY this shape:

{{
  "executive_name": "string",
  "greetings": ["string", ...],
  "closings": ["string", ...],
  "customer_mood": "Frustrated" | "Concerned" | "Satisfied" | "Neutral",
  "mood_reason": "string",
  "executive_tone": "Helpful" | "Helpful but Defensive" | "Defensive" | "Neutral",
  "keywords": ["string", ...],
  "business_facts": {{
    "issues": ["string", ...],
    "requests": ["string", ...],
    "actions": ["string", ...]
  }},
  "other_topics": ["string", ...]
}}

Output JSON only.
"""

        analysis_start = time.time()
        response = safe_generate(
            deepseek_client,
            model=ANALYSIS_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2500,
            json_mode=True,
        )
        try:
            usage = response.usage
            cost_info = calculate_deepseek_cost(usage)


            prompt_tokens = cost_info["input_tokens"]
            response_tokens = cost_info["output_tokens"]
            thinking_tokens = cost_info["thinking_tokens"]
            deepseek_usage_total = add_deepseek_usage(
                deepseek_usage_total,
                cost_info
            )

            print("\n" + "=" * 60)
            print("DEEPSEEK ANALYSIS COST")
            print("=" * 60)
            print("Input Tokens    :", prompt_tokens)
            print("Output Tokens   :", response_tokens)
            print("Total Tokens    :", cost_info["total_tokens"])
            print("Cost USD        : $%.6f" % cost_info["cost_usd"])
            print("Cost INR        : ₹%.4f" % cost_info["cost_inr"])
            print("=" * 60)

            total_tokens = prompt_tokens + response_tokens

            try:
                finish_reason = response.choices[0].finish_reason
                print("FINISH REASON:", finish_reason)
            except Exception as fe:
                print("Could not get finish_reason:", fe)

        except Exception as e:
            print("Token usage not available:", e)
            prompt_tokens = response_tokens = thinking_tokens = total_tokens = 0

        print("ANALYSIS CONTENT LENGTH:",
            len(response.choices[0].message.content or ""))

        print("ANALYSIS FINISH REASON:",
            response.choices[0].finish_reason)


        


        raw_output = (response.choices[0].message.content or "").strip()
        print(f"[TIMER] Analysis (DeepSeek) took {time.time() - analysis_start:.2f} seconds")
        save_text(raw_output, audio_path, "_deepseek_raw_response.txt")
        try:
            print("\nRAW DEEPSEEK OUTPUT:\n")
            print(raw_output)
            print("\nEND OUTPUT\n")
        except UnicodeEncodeError:
            print("\nRAW DEEPSEEK OUTPUT: (contains non-ASCII characters, skipped)\n")

        print("HAS BUSINESS FACTS:", '"business_facts"' in raw_output)
        data = robust_parse(raw_output)
        print("RAW EXECUTIVE TONE:", repr(data.get("executive_tone")))
        tone = data.get("executive_tone", "").lower()

        if "defensive" in tone:
            data["executive_tone"] = "Helpful but Defensive"
        elif tone:
            data["executive_tone"] = "Helpful"
        else:
            data["executive_tone"] = "Neutral"

        mood = data.get("customer_mood", "Neutral")
        customer_text_lower = extract_speaker_text(transcript, "Customer")

        anger_tone_signals = [
            "[shouting]",
            "चिडलो", "चिडले", "गुस्सा", "गुस्से में",
            "irritated", "angry", "annoyed", "fed up",
            "bakwas", "बकवास", "नालायक", "नालायक आहेस",
        ]

        recurring_issue_signals = [
            "again", "every time", "always", "still not", "not working",
            "2 months", "3 months", "again and again", "complaint number",
            "काम नाही", "परत", "नाही होत", "बंद आहे",
            "नाही चालत", "होत नाही", "का नाही",
            "क्यों नहीं", "नहीं हो रहा", "नहीं आ रहा",
            "kab tak", "kitne din", "bahut time",
            "feature नाहीये", "option नाहीये", "missing",
        ]

        MIN_CUSTOMER_CHARS = 15

        if len(customer_text_lower.strip()) < MIN_CUSTOMER_CHARS:
            data["customer_mood"] = "Mood Not Determined"
            data["mood_reason"] = (
                "Customer's speech could not be clearly separated from the "
                "Executive's in this call, so mood could not be reliably judged."
            )
            print("[Mood sanity] Too little Customer-labeled speech — "
                  "marking mood as 'Mood Not Determined' instead of guessing.")
        else:
            has_anger_tone = any(s in customer_text_lower for s in anger_tone_signals)
            has_recurring_issue = any(s in customer_text_lower for s in recurring_issue_signals)

            if has_anger_tone:
                data["customer_mood"] = "Frustrated"
                data["mood_reason"] = "Customer's tone showed shouting/anger/abusive language during the call."
            elif mood == "Frustrated" and not has_anger_tone:
                if has_recurring_issue:
                    data["customer_mood"] = "Concerned"
                    data["mood_reason"] = "Customer calmly reported a recurring issue or missing feature, without anger."
                else:
                    data["customer_mood"] = "Neutral"
                    data["mood_reason"] = "Customer tone was calm throughout the call."
            elif mood == "Concerned" and not has_recurring_issue:
                data["customer_mood"] = "Neutral"
                data["mood_reason"] = "No recurring issue or complaint detected in transcript."

        # -------- GREETINGS / CLOSINGS SANITY FILTER --------
        VALID_GREETINGS = {
            "hello", "hi", "good morning",
            "good afternoon", "good evening",
            "namaste", "नमस्ते",
            "हॅलो", "हेलो",
            "hare krishna", "हरे कृष्ण", "हरे कृष्णा",
            "jai shree krishna", "जय श्री कृष्ण",
            "jai hind",
            "radhe radhe",
            "गुड मॉर्निंग", "गूड मॉर्निंग", "गुड मार्निंग",
            "गुड आफ्टरनून", "गूड आफ्टरनून",
            "गुड इव्हिनिंग", "गूड इव्हिनिंग", "गुड इवनिंग"
        }

        VALID_CLOSINGS = {
            "ok", "okay", "ठीक आहे",
            "ओके", "thank you",
            "thanks", "bye",
            "धन्यवाद", "थँक यू", "थँक्यू", "थैंक यू", "थैंक्यू"
        }

        HONORIFICS = {"sir", "madam", "सर", "मॅडम"}

        GREETING_PHRASE_TUPLES = sorted(
            (tuple(g.split()) for g in VALID_GREETINGS),
            key=len, reverse=True
        )

        def match_greeting_phrases(words):
            matched = []
            i, n = 0, len(words)
            while i < n:
                matched_here = False
                for phrase in GREETING_PHRASE_TUPLES:
                    plen = len(phrase)
                    if plen and tuple(words[i:i + plen]) == phrase:
                        matched.append(" ".join(phrase))
                        i += plen
                        matched_here = True
                        break
                if not matched_here:
                    i += 1
            return matched if matched else None

        def _clean_word(w):
            return w.strip(" ।.,!?\"'()")

        clean_greetings = []
        for g in data.get("greetings", []):
            g_lower = g.lower().strip()
            words = [_clean_word(w) for w in g_lower.split() if _clean_word(w)]

            if len(words) > 25 or not words:
                continue

            core_words = [w for w in words if w not in HONORIFICS]
            if not core_words:
                continue

            matched = match_greeting_phrases(core_words)
            if matched:
                clean_greetings.extend(matched)
            elif core_words[0] in VALID_GREETINGS:
                clean_greetings.append(core_words[0])

        if not clean_greetings:
            clean_greetings = ["Hello"]

        data["greetings"] = list(dict.fromkeys(clean_greetings))[:5]

        clean_closings = []
        for c in data.get("closings", []):
            c_lower = _clean_word(c.lower().strip())
            words = c_lower.split()
            if len(words) > 5 or not words:
                continue
            if (
                c_lower in VALID_CLOSINGS or
                any(c_lower.startswith(x) for x in VALID_CLOSINGS)
            ):
                clean_closings.append(c_lower)

        data["closings"] = list(dict.fromkeys(clean_closings))[:3]

        transcript_lower = transcript.lower()
        def keyword_match(kw, transcript_text, fuzzy_threshold=0.82, proximity=30):
            kw_norm = kw.lower().strip()
            text_norm = transcript_text.lower().strip()

            if not kw_norm or not text_norm:
                return False

            if kw_norm in text_norm:
                return True

            keyword_words = [w for w in re.split(r'\W+', kw_norm) if w]
            transcript_words = [w for w in re.split(r'\W+', text_norm) if w]

            if not keyword_words:
                return False

            def word_matches(keyword_word, transcript_word):
                if len(keyword_word) <= 3:
                    return keyword_word == transcript_word
                ratio = difflib.SequenceMatcher(None, keyword_word, transcript_word).ratio()
                return ratio >= fuzzy_threshold

            if len(keyword_words) == 1:
                keyword_word = keyword_words[0]
                return keyword_word in transcript_words

            positions_per_word = []
            for keyword_word in keyword_words:
                positions = [
                    i for i, transcript_word in enumerate(transcript_words)
                    if word_matches(keyword_word, transcript_word)
                ]
                if not positions:
                    return False
                positions_per_word.append(positions)

            for combo in itertools.product(*positions_per_word):
                if max(combo) - min(combo) <= proximity:
                    return True

            return False
        # def keyword_match(kw, transcript_text, fuzzy_threshold=0.82):
        #     """
        #         Match a keyword against the Whisper transcript.

        #         Supports:
        #         - Exact phrase
        #         - Small spelling mistakes
        #         - Whisper variations

        #         Example:
        #         Loss Sale Report
        #         Lost Sale Report
        #         Last Sale Report

        #         All can match the canonical keyword:
        #         Loss Sale Report
        #     """

        #     kw_norm = kw.lower().strip()
        #     text_norm = transcript_text.lower().strip()

        #     if not kw_norm or not text_norm:
        #         return False


        #     if kw_norm in text_norm:
        #         return True

    
        #     keyword_words = [
        #         w for w in re.split(r'\W+', kw_norm)
        #         if w
        #     ]

        #     transcript_words = [
        #         w for w in re.split(r'\W+', text_norm)
        #         if w
        #     ]

        #     if not keyword_words:
        #         return False

    
        #     if len(keyword_words) == 1:
        #         keyword_word = keyword_words[0]

        #         # Single-word keywords must be explicitly present.
        #         # Do NOT use fuzzy matching because it creates random keywords.
        #         return keyword_word in transcript_words


        #     window_size = len(keyword_words)

        #     for i in range(
        #         len(transcript_words) - window_size + 1
        #     ):

        #         window = transcript_words[
        #             i:i + window_size
        #         ]

        #         match_count = 0

        #         for keyword_word, transcript_word in zip(
        #             keyword_words,
        #             window
        #         ):

            
        #             if len(keyword_word) <= 3:

        #                 if keyword_word == transcript_word:
        #                     match_count += 1

        #             else:

        #                 ratio = difflib.SequenceMatcher(
        #                     None,
        #                     keyword_word,
        #                     transcript_word
        #                 ).ratio()

        #                 if ratio >= fuzzy_threshold:
        #                     match_count += 1

        #         if match_count == len(keyword_words):
        #             return True

        #     return False

        #detected_keywords = [kw for kw in keywords_list if keyword_match(kw)]
        detected_keywords = [
            kw for kw in keywords_list
            if keyword_match(kw, transcript)
        ]
        print("\n========== KEYWORD DEBUG ==========")
        print("TRANSCRIPT:")
        print(transcript)

        print("\nKEYWORDS LIST:")
        print(keywords_list)

        print("\nDETECTED KEYWORDS:")
        print(detected_keywords)

        print("===================================\n")

        # keywords_list_lower = {k.lower(): k for k in keywords_list}
        # model_keywords = [
        #     keywords_list_lower[k.lower()]
        #     for k in data.get("keywords", [])
        #     if k.lower() in keywords_list_lower
        #     and keyword_match(keywords_list_lower[k.lower()])
        # ]

        # final_keywords = list(dict.fromkeys(model_keywords + detected_keywords))
        final_keywords = list(dict.fromkeys(detected_keywords))

        if not final_keywords:
            final_keywords = ["No matching keyword detected"]

        data["keywords"] = final_keywords

        if not final_keywords:
            final_keywords = ["No matching keyword detected"]

        data["keywords"] = final_keywords

        facts = data.get("business_facts", {})
        print("\n========== BUSINESS FACTS DEBUG ==========")
        print(json.dumps(facts, ensure_ascii=False, indent=2))
        print("===========================================\n")
        requests = facts.get("requests", [])
        actions = facts.get("actions", [])

        fixed_requests = []
        fixed_actions = []

        for item in requests:
            text = item.lower()

            if any(x in text for x in [
                "callback after 10",
                "callback in 10",
                "keep machine connected",
                "machine remain connected",
                "remain connected"
            ]):
                fixed_actions.append(
                    "Executive asked customer to keep the machine connected for 10 minutes."
                )
                fixed_actions.append(
                    "Executive informed customer that a callback will be done after 10 minutes."
                )
                continue

            fixed_requests.append(item)

        for item in actions:
            text = item.lower()

            if any(x in text for x in [
                "asked for system id",
                "requested system id",
                "asked for password"
            ]):
                fixed_actions.append(
                    "Executive requested customer system ID and password for troubleshooting."
                )
                continue

            fixed_actions.append(item)

        facts["requests"] = list(dict.fromkeys(fixed_requests))
        facts["actions"] = list(dict.fromkeys(fixed_actions))

        data["business_facts"] = facts

        # --- Prefer known-name matching (reliable, standardized spelling) ---
        known_name = detect_known_executive(transcript)
        if known_name:
            data["executive_name"] = known_name
        else:
            name = data.get("executive_name", "Not mentioned")
            if name and name.lower() not in ["not mentioned", "not detected", "n/a"]:
                if name.lower() in transcript.lower():
                    data["executive_name"] = name
                else:
                    data["executive_name"] = "Not mentioned"
            else:
                data["executive_name"] = "Not mentioned"

        def to_s(v):
            return ", ".join(v) if isinstance(v, list) and v else str(v or "None detected")

        customer_mood = data.get("customer_mood", "Neutral")
        mood_reason = data.get("mood_reason", "N/A")
        executive_tone = data.get("executive_tone", "Normal")

        analysis_text = f"Customer Mood: {customer_mood} ({mood_reason})\n"
        analysis_text += f"Executive Name: {data.get('executive_name', 'Not mentioned')}\n"
        analysis_text += f"Greetings: {to_s(data.get('greetings'))}\n"
        analysis_text += f"Closings: {to_s(data.get('closings'))}\n"
        analysis_text += f"Executive Tone: {executive_tone}"

        short_summary, long_summary, summary_usage = generate_detailed_summary(
            deepseek_client,
            clean_transcript,
            data.get("business_facts", {}),
            data.get("executive_name", "Not mentioned"),
            customer_mood,
            mood_reason
        )

        deepseek_usage_total = add_deepseek_usage(
            deepseek_usage_total,
            summary_usage
        )
        combined_summary = (
            "SHORT SUMMARY\n"
            + "=" * 50
            + "\n"
            + short_summary
            + "\n\nLONG SUMMARY\n"
            + "=" * 50
            + "\n"
            + long_summary
        )
        print(f"[TIMER] TOTAL analyze_audio_with_gemini took {time.time() - total_start:.2f} seconds")
        token_info = {
            "prompt": deepseek_usage_total["input_tokens"],
            "response": deepseek_usage_total["output_tokens"],
            "thinking": deepseek_usage_total["thinking_tokens"],
            "total": deepseek_usage_total["total_tokens"],

            "deepseek_input": deepseek_usage_total["input_tokens"],
            "deepseek_output": deepseek_usage_total["output_tokens"],
            "deepseek_thinking": deepseek_usage_total["thinking_tokens"],
            "deepseek_total": deepseek_usage_total["total_tokens"],
            "deepseek_cost_usd": deepseek_usage_total["cost_usd"],
            "deepseek_cost_inr": deepseek_usage_total["cost_inr"]
        }
        return (
            transcript,
            raw_output,
            format_facts(data.get("business_facts")),
            analysis_text,
            combined_summary,
            to_s(data.get("keywords")),
            token_info
        )
    finally:
        pass


def robust_parse(raw):
    try:
        return parse_llm_json(raw)
    except Exception:
        try:
            return json.loads(clean_json_string(raw))
        except Exception:
            def get_s(k):
                return (
                    re.findall(rf'"{k}"\s*:\s*"(.*?)"', raw, re.DOTALL)
                    or ["Not detected"]
                )[0]

            def get_a(k):
                m = re.search(rf'"{k}"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
                if m:
                    return [x.strip().strip('"').strip("'") for x in m.group(1).split(",") if x.strip()]
                return []

            return {
                "executive_name": get_s("executive_name"),
                "greetings": get_a("greetings"),
                "closings": get_a("closings"),
                "customer_mood": get_s("customer_mood"),
                "mood_reason": get_s("mood_reason"),
                "executive_tone": get_s("executive_tone"),
                "keywords": get_a("keywords"),
                "business_facts": {
                    "issues": get_a("issues"),
                    "requests": get_a("requests"),
                    "actions": get_a("actions")
                },
                "short_summary": get_s("short_summary"),
                "summary": get_s("summary")
            }


def save_transcript(transcript, audio_path):
    with open(os.path.splitext(audio_path)[0] + "_transcript.txt", "w", encoding="utf-8") as f:
        f.write(transcript)


def save_text(text, audio_path, suffix):
    with open(os.path.splitext(audio_path)[0] + suffix, "w", encoding="utf-8") as f:
        f.write(text)


def save_summary(summary, analysis, facts, keywords, audio_path):
    with open(os.path.splitext(audio_path)[0] + "_summary.txt", "w", encoding="utf-8") as f:
        f.write(
            "=" * 50 + "\nANALYSIS\n" + "=" * 50 + f"\n\n{analysis}\n\n"
            + "=" * 50 + "\nKEYWORDS IDENTIFIED\n" + "=" * 50 + f"\n\n{keywords}\n\n"
            + "=" * 50 + "\nBUSINESS FACTS\n" + "=" * 50 + f"\n\n{facts}\n\n"
            + "=" * 50 + "\nSUMMARY\n" + "=" * 50 + f"\n\n{summary}"
        )