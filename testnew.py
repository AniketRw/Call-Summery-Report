from google import genai
from google.genai import types
tk = None
import re
import threading
import subprocess
import os
import time
import json
import sys

USD_TO_INR = 95.0

INPUT_PRICE_PER_1M = 1.50
OUTPUT_PRICE_PER_1M = 9.00

print("IMPORTING testnew.py")
print("PID:", os.getpid())
from faster_whisper import WhisperModel


def calculate_gemini_cost(usage):

    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_inr": 0.0
        }

    input_tokens = getattr(
        usage, "prompt_token_count", 0
    ) or 0

    output_tokens = getattr(
        usage, "candidates_token_count", 0
    ) or 0

    thinking_tokens = getattr(
        usage, "thoughts_token_count", 0
    ) or 0

    input_cost_usd = (
        input_tokens / 1_000_000
    ) * INPUT_PRICE_PER_1M

    output_cost_usd = (
        output_tokens / 1_000_000
    ) * OUTPUT_PRICE_PER_1M

    total_cost_usd = (
        input_cost_usd + output_cost_usd
    )

    total_cost_inr = (
        total_cost_usd * USD_TO_INR
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "total_tokens": (
            input_tokens +
            output_tokens +
            thinking_tokens
        ),
        "cost_usd": total_cost_usd,
        "cost_inr": total_cost_inr
    }


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
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
#GEMINI_MODEL = "models/gemini-2.5-flash"
TRANSCRIPT_MODEL = "models/gemini-3.5-flash"
print("Loading Whisper model...")

whisper_model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)

print("Whisper model loaded.")

ANALYSIS_MODEL = "models/gemini-3.5-flash"
SUMMARY_SEED = 12345

TRANSCRIPT_CORRECTIONS = {
    "loss sheet report": "loss sale report",
    "loss seal report": "loss sale report",
    "los sale report": "loss sale report",
    "loss sell report": "loss sale report",
    "lost sale report": "loss sale report",
    "loss cell report": "loss sale report",
}
def whisper_transcribe(audio_path):
    print(">>> ENTERED LOCAL WHISPER TRANSCRIPTION", flush=True)
    print("Audio exists:", os.path.exists(audio_path), flush=True)
    print("Audio size:", os.path.getsize(audio_path), "bytes", flush=True)
    print("Audio path:", audio_path, flush=True)

    segments, info = whisper_model.transcribe(
        audio_path,
        language="hi",
        beam_size=5,
        vad_filter=False,
        vad_parameters=dict(min_silence_duration_ms=500, threshold=0.3)
    )

    print("Language:", info.language, "| Probability:", info.language_probability, flush=True)

    lines = []
    segment_count = 0
    for segment in segments:
        segment_count += 1
        print("WHISPER SEGMENT:", segment.start, segment.end, repr(segment.text), flush=True)
        m = int(segment.start // 60)
        s = int(segment.start % 60)
        lines.append(f"[{m:02d}:{s:02d}] {segment.text.strip()}")

    print("WHISPER SEGMENT COUNT:", segment_count, flush=True)
    return "\n".join(lines)

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
    "Akshay": ["akshay", "अक्षय"],          # ← Devanagari added
    "Suresh": ["suresh", "सुरेश"],
    "Avinash": ["avinash", "अविनाश"],
    "Amol": ["amol", "अमोल"],
    "Mahendra": ["mahendra", "महेंद्र"],
    "Dilip": ["dilip", "deelip", "दिलीप"],
    "Dipak": ["dipak", "deepak", "दीपक"],
    "Gautam": ["gautam", "गौतम"],
    "Sagar" : ["sagar", "सागर"],
    "Sandeep Gosavi" : ["sandeep gosavi" , "गोसावी संदीप"],
    "Rajesh" :["rajesh", "राजेश "],
    "Akash" : ["akash", "आकाश "],
    "Kishor" : ["kishor" , "किशोर"],
    "Sandeep" : ["sandeep" , "संदीप"],
    "Prathmesh" : ["prathmesh" , "प्रथमेश"],
    "Pratik" : ["pratik" , "प्रतीक"],
    "Abhijit" : ["abhijit" , "अभिजीत"],
    "Aniket" : ["aniket", "अनिकेत"],


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
    # company/connector words — हे कधीच नाव नसतात
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
    # Devanagari किंवा Latin दोन्हीत योग्य capitalize कर
    if re.match(r'^[a-zA-Z]+$', raw_name.strip()):
        return raw_name.strip().title()
    return raw_name.strip()

import re
def fix_speaker_consistency(transcript: str) -> str:
    import re

    lines = transcript.splitlines()
    fixed = []

    prev_speaker = None
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

        # -------------------------
        # NAME QUESTION
        # -------------------------

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

        # -------------------------
        # PASSWORD
        # -------------------------

        elif "पासवर्ड" in lower:
            speaker = "Executive"

        elif (
            "पासवर्ड" in prev_text
            and len(text.split()) <= 5
        ):
            speaker = "Customer"

        # -------------------------
        # ID
        # -------------------------

        elif "आईडी" in lower or "id" in lower:
            speaker = "Executive"

        elif (
            ("आईडी" in prev_text or "id" in prev_text)
            and len(text.split()) <= 5
        ):
            speaker = "Customer"

        # -------------------------
        # COMPLAINT NUMBER
        # -------------------------

        elif (
            "कंप्लेंट नंबर" in lower
            or "complaint number" in lower
        ):
            speaker = "Executive"

        elif (
            "कंप्लेंट नंबर" in prev_text
            and len(text.split()) <= 5
        ):
            speaker = "Customer"

        fixed.append(
            f"{ts} {speaker}{aside}: {text}"
        )

        prev_speaker = speaker
        prev_text = text

    return "\n".join(fixed)

def extract_speaker_text(transcript, speaker="Customer"):
    """Extract ONLY the given speaker's actual dialogue (skipping [Aside]
    lines), so mood/tone keyword checks don't get polluted by the other
    speaker's words or by background chatter."""
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

    # Priority 1: explicit self-introduction — "X बोलतोय", "This is X speaking" इ.
    # dictionary मध्ये नाव नसलं तरी directly तेच नाव वापरायचं (normalize फक्त spelling साठी)
    self_intro_patterns = [
        r"([a-zA-Z\u0900-\u097F]+)\s*बोलतोय",
        r"([a-zA-Z\u0900-\u097F]+)\s*बोल रहा",
        r"this is\s+([a-zA-Z]+)\s+speaking",
        r"मेरा नाम\s+([a-zA-Z\u0900-\u097F]+)",
        r"माझं नाव\s+([a-zA-Z\u0900-\u097F]+)",
        # "रिटेलवेअर कडून/कडनं ... बोलतोय ... सर, <Name>" पॅटर्न
        r"(?:रिटेलवेअर|retailware)[^,।\n]{0,25}?(?:कडून|कडनं)[^,।\n]{0,25}?"
        r"बोलतोय[^,।\n]{0,10}?(?:सर|मॅडम)?\s*,?\s*([a-zA-Z\u0900-\u097F]+)",
    ]
    for pat in self_intro_patterns:
        m = re.search(pat, transcript, flags=re.IGNORECASE)
        if m:
            normalized = normalize_executive_name(m.group(1))
            if normalized:
                return normalized

    # Priority 2: fallback — known dictionary substring match
    # (loose/generic match इथे नाही कारण customer चं नावही चुकून पकडलं जाऊ शकतं)
    for standard_name, variants in KNOWN_EXECUTIVE_NAMES.items():
        for variant in variants:
            if re.search(r'\b' + re.escape(variant.lower()) + r'\b', transcript_lower):
                return standard_name
    return None



def get_google_client():
    if not GOOGLE_API_KEY:
        raise RuntimeError("Set GOOGLE_API_KEY in your .env file before running this app.")
    return genai.Client(api_key=GOOGLE_API_KEY)
def safe_generate(client, **kwargs):
    max_retries = 6
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as e:
            err = str(e).lower()
            print(f"Retry {attempt+1}/{max_retries}: {e}")
            retryable = any(x in err for x in [
                "server disconnected", "timeout", "503",
                "connection", "429", "internal", "unavailable"
            ])
            if not retryable:
                raise
            if attempt == max_retries - 1:
                raise
            wait = 3 * (attempt + 1)   # 5s, 10s, 15s, 20s, 25s
            print(f"Waiting {wait}s before retry...")
            time.sleep(wait)



# def convert_audio(input_path):
#     output_path = os.path.splitext(input_path)[0] + "_16k.wav"
#     result = subprocess.run([
#         "ffmpeg", "-y",
#         "-hide_banner",
#         "-loglevel", "error",
#         "-i", input_path,
#         "-vn",
#         "-ac", "1",
#         "-ar", "16000",
#         "-c:a", "pcm_s16le",
#         output_path
#     ], capture_output=True, text=True)
#     if result.returncode != 0:
#         raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[:300]}")
#     return output_path

def find_speech_start(wav_path):
    """
    Detect where ring-back tone ends and speech begins.
    wav_path OGG असू शकतो, म्हणून आधी तो तात्पुरता WAV मध्ये decode करतो.
    """
    import wave, struct, subprocess, os
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

        samples = np.array(struct.unpack('<' + 'h' * (len(data)//2), data), dtype=np.float32)
        step = int(framerate * 0.5)
        ring_pcts = []
        for i in range(0, len(samples) - step, step):
            seg = samples[i:i+step]
            rms = float(np.sqrt(np.mean(seg**2)))
            if rms < 200:
                ring_pcts.append(None)
                continue
            fft   = np.abs(np.fft.rfft(seg))
            freqs = np.fft.rfftfreq(step, 1.0/framerate)
            ring_mask   = (freqs >= 300) & (freqs <= 500)
            speech_mask = (freqs >= 100) & (freqs <= 3400) & ~ring_mask
            ring_e   = float(np.sum(fft[ring_mask]))
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



def convert_audio(input_path):
    """Convert audio to 16 kHz mono WAV, trimming ring-back tone if present."""
    start_time = time.time()
    #output_path = os.path.splitext(input_path)[0] + "_16k.wav"
    output_path = os.path.splitext(input_path)[0] + "_16k.ogg" 
    # Step 1: standard conversion
    # result = subprocess.run([
    #     "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    #     "-i", input_path,
    #     "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
    #     output_path
    # ], capture_output=True, text=True)
    result = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", "96k",
        output_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[:300]}")

    # Step 2: trim ring-back tone
    speech_start = find_speech_start(output_path)
    if speech_start > 2.0:                   # only trim if >2s of ring detected
        trimmed = os.path.splitext(input_path)[0] + "_trimmed.wav"
        r2 = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", output_path,
            "-ss", str(speech_start),
            "-c", "copy", trimmed
        ], capture_output=True, text=True)
        if r2.returncode == 0:
            os.replace(trimmed, output_path)
            print(f"[Ring trim] Removed {speech_start:.1f}s of ring tone")
        else:
            print(f"[Ring trim] Trim step failed, using full audio")

    elapsed = time.time() - start_time
    print(f"[TIMER] convert_audio took {elapsed:.2f} seconds")
    return output_path


def parse_gemini_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"): text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1: text = text[start:end+1]
    return json.loads(text)

def clean_json_string(raw_text):
    import re
    text = raw_text.strip()
    def escape_inside_quotes(match):
        return match.group(0).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return re.sub(r'"(?:\\.|[^"\\])*"', escape_inside_quotes, text)

def format_facts(facts):
    if not isinstance(facts, dict): return str(facts or "").strip()
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
    previous line — fixes cases where Gemini inserts a stray newline
    in the middle of a single logical line."""
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
    """Detects short repeating cycles (e.g. A,B,A,B,A,B...) of length 1-4 lines
    that repeat many times in a row, and collapses them."""
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
            cycle = contents[i:i+period]
            if any(c is None for c in cycle):
                continue
            repeats = 1
            j = i + period
            while j + period <= n and contents[j:j+period] == cycle:
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

def get_audio_transcripts_with_gemini(client, uploaded_file):
    print(">>> ENTERED get_audio_transcripts_with_gemini", flush=True)
    start_time = time.time()
    prompt = (
    "Transcribe this support call exactly. Add [MM:SS] timestamps. "
    "Identify speakers as 'Executive' and 'Customer'. "
    "The Executive usually opens the call with a self-introduction naming "
    "the company 'Retailware' (may sound like 'रिटेलवेअर', 'रिटेल वेअर', 'रिटेल वाला', "
    "or similar phonetic variants) — do NOT transcribe this as unrelated words like "
    "'children' (चिल्ड्रन) or any other common word. If unclear, prefer 'Retailware'. "

    "SPEAKER IDENTITY RULES: "
    "The Executive is the support/helpdesk agent. Establish who is who EARLY using clues like: "
    "company/product name self-introduction (e.g. 'रिटेलWARE  बोलतोय'), being addressed respectfully by name/title, "
    "or asking troubleshooting questions. Once established, KEEP that speaker's label consistent by voice "
    "for the entire call, even through noisy or unclear sections. "
    "If audio is unclear, silent, noisy, or overlapping and you cannot confidently tell who is speaking, "
    "do NOT guess or alternate the speaker label —  "
    "Never change a speaker unless there is clear conversational evidence."
    "If one speaker asks a question and the next short reply logically answers it,keep that reply with the opposite speaker."
    "Do not alternate speakers randomly."
    "Keep conversation turns intact."
    "or write [MM:SS] [Unclear speaker] and skip to the next distinct segment. "
    "ASIDE / BACKGROUND SPEECH RULE: "
    "Sometimes a speaker (usually the Customer, who is often physically at a shop/counter) briefly talks to "
    "someone else nearby instead of the other person on the call — e.g. asking a bystander for coins/change, "
    "giving an unrelated instruction to staff, calling out a name, or a side remark unrelated to the support issue. "
    "Watch carefully for ANY of these signs (even just ONE is enough to mark it as an aside): "
    "1) The sentence has NO connection to the support topic (ID, password, scanning, billing, label, rate, "
    "troubleshooting, login) — e.g. random names being called out, instructions to staff/family, casual chit-chat. "
    "2) The audio sounds farther from the phone / muffled / echoey / lower volume compared to the speaker's "
    "normal speaking level for this call. "
    "3) The sentence is a short one-word or few-word utterance directed at someone else, not phrased as if "
    "talking to the person on the call (e.g. missing the polite tone or 'sir/ji' typically used with the caller). "
    "4) There is a brief overlapping voice, background chatter, or a second person's voice audible along with "
    "the main speaker. "
    "Err on the side of marking something as [Aside] if you are even moderately confident it fits — it is better "
    "to over-mark an aside than to silently merge unrelated background speech into the main conversation. "
    "If you detect this, label the line with the speaker's normal role but ADD the tag [Aside] right after the "
    "role, like '[MM:SS] Customer [Aside]: ...'. Do NOT let an aside change who you think is the Executive or "
    "Customer for the rest of the call — the role identity stays the same, only this one line is marked as an aside. "
    "IMPORTANT: In brackets, note the tone or volume if it changes significantly "
    "(e.g., [Loudly], [Shouting], [Silently], [Calmly]). "
    "Keep the original language (Hindi/Marathi/English) as spoken. "
    "Use Devanagari script for Hindi/Marathi and Latin script for English. "
    "If there is silence, hold music, or unclear/repetitive background audio for "
    "an extended period, do NOT repeat the same word or number over and over. "
    "Instead write a single line like [MM:SS] [Hold music / silence] and move to "
    "the next distinct speech segment."
)

    max_attempts = 3
    raw_text = ""
    for attempt in range(max_attempts):
        temp = 0 if attempt == 0 else 0.2 + (0.15 * (attempt - 1))  # 0 → 0.2 → 0.35
        response = safe_generate(
            client,
            model=TRANSCRIPT_MODEL,
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                temperature=temp,
                max_output_tokens=12000
            )
        )

        try:
            transcript_usage = response.usage_metadata
            transcript_cost_info = calculate_gemini_cost(transcript_usage)

            print("\n" + "=" * 60)
            print("GEMINI TRANSCRIPTION COST")
            print("=" * 60)

            print("Input Tokens    :", transcript_cost_info["input_tokens"])
            print("Output Tokens   :", transcript_cost_info["output_tokens"])
            print("Thinking Tokens :", transcript_cost_info["thinking_tokens"])
            print("Total Tokens    :", transcript_cost_info["total_tokens"])
            print("Cost USD        : $%.6f" % transcript_cost_info["cost_usd"])
            print("Cost INR        : ₹%.4f" % transcript_cost_info["cost_inr"])

            if hasattr(transcript_usage, "total_token_count"):
                print("SDK Total Tokens:", transcript_usage.total_token_count)

            print("=" * 60)

        except Exception as e:
            print("Transcription token usage not available:", e)
       
        candidate_text = (response.text or "").strip()
        try:
            finish_reason = response.candidates[0].finish_reason
        except Exception:
            finish_reason = None
        print(f"[Transcription] finish_reason={finish_reason}")

        is_degenerate = is_degenerate_text(candidate_text)
        if not is_degenerate:
            raw_text = candidate_text
            print(f"[Degeneration check] Clean output on attempt {attempt+1} (temp={temp})")

            # Agar output token-limit mule truncate zala, continuation mागव
            if str(finish_reason) in ("MAX_TOKENS", "FinishReason.MAX_TOKENS"):
                print("[Continuation] Truncated output detected, requesting continuation...")
                cont_prompt = (
                    "Continue the transcription EXACTLY from where it stopped. "
                    "Do not repeat any earlier lines. Continue using the same "
                    "[MM:SS] timestamp format and speaker/aside rules as before.\n\n"
                    "Transcript so far (last part, for reference only, do not repeat):\n"
                    + raw_text[-800:]
                )
                cont_response = safe_generate(
                    client,
                    model=TRANSCRIPT_MODEL,
                    contents=[uploaded_file, cont_prompt],
                    config=types.GenerateContentConfig(
                        temperature=temp,
                        max_output_tokens=10000
                    )
                )
                
                try:
                    cont_usage = cont_response.usage_metadata
                    cont_cost_info = calculate_gemini_cost(cont_usage)

                    print("\n" + "=" * 60)
                    print("GEMINI TRANSCRIPTION CONTINUATION COST")
                    print("=" * 60)

                    print("Input Tokens    :", cont_cost_info["input_tokens"])
                    print("Output Tokens   :", cont_cost_info["output_tokens"])
                    print("Thinking Tokens :", cont_cost_info["thinking_tokens"])
                    print("Total Tokens    :", cont_cost_info["total_tokens"])
                    print("Cost USD        : $%.6f" % cont_cost_info["cost_usd"])
                    print("Cost INR        : ₹%.4f" % cont_cost_info["cost_inr"])

                    if hasattr(cont_usage, "total_token_count"):
                        print("SDK Total Tokens:", cont_usage.total_token_count)

                    print("=" * 60)

                except Exception as e:
                    print("Continuation token usage not available:", e)
                cont_text = (cont_response.text or "").strip()
                if cont_text and not is_degenerate_text(cont_text):
                    raw_text = raw_text + "\n" + cont_text
                    print("[Continuation] Appended continued transcript.")
            break
        else:
            print(f"[Degeneration check] Attempt {attempt+1} (temp={temp}) hit a repetition loop, retrying...")
            raw_text = candidate_text  # keep last attempt in case all fail

    raw_text = clean_degenerate_output(raw_text)
    with open("debug_raw_transcript.txt", "w", encoding="utf-8") as f:
        f.write(raw_text)  
    raw_text = merge_orphan_lines(raw_text)
    elapsed = time.time() - start_time
    print(f"[TIMER] Transcription (Gemini) took {elapsed:.2f} seconds")
    corrected = apply_corrections(raw_text)
    corrected = fix_speaker_consistency(corrected)
    corrected = collapse_alternating_filler(corrected)
    corrected = collapse_repeated_lines(corrected)
    return collapse_repeated_lines(corrected)


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
    """Second, focused pass: generate ONLY the short + long summary,
    using the already-extracted facts as grounding context. Keeping this
    separate from the big extraction call means the model isn't juggling
    9 different fields at once — it can focus purely on writing a coherent,
    well-structured summary."""

    facts_str = format_facts(business_facts)

    prompt = f"""
Using the transcript and the already-extracted facts below, write a clear summary of this support call.

Transcript:
{clean_transcript}

Known facts (already verified — use these, don't re-derive):
- Executive: {executive_name}
- Customer mood: {customer_mood} ({mood_reason})
{facts_str}

Write:
1. short_summary: 2-3 sentences covering why the customer called, the main issue, and current status/outcome.
2. summary: A detailed 4-6 sentence summary covering the issue, what the executive did, troubleshooting/guidance given, resolution status, and any pending follow-up.

Rules:
- Do NOT reverse who did what — Executive helps/troubleshoots, Customer reports/requests.
- Be specific and factual, grounded only in the transcript and facts given above.
- No generic filler sentences.

Return valid JSON only, in this exact format:
{{"short_summary": "...", "summary": "..."}}
"""

    response = safe_generate(
        client,
        model=ANALYSIS_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            max_output_tokens=2000,
        )
    )

    try:
        summary_usage = response.usage_metadata
        summary_cost_info = calculate_gemini_cost(summary_usage)

        print("\n" + "=" * 60)
        print("GEMINI DETAILED SUMMARY COST")
        print("=" * 60)

        print("Input Tokens    :", summary_cost_info["input_tokens"])
        print("Output Tokens   :", summary_cost_info["output_tokens"])
        print("Thinking Tokens :", summary_cost_info["thinking_tokens"])
        print("Total Tokens    :", summary_cost_info["total_tokens"])
        print("Cost USD        : $%.6f" % summary_cost_info["cost_usd"])
        print("Cost INR        : ₹%.4f" % summary_cost_info["cost_inr"])

        if hasattr(summary_usage, "total_token_count"):
            print("SDK Total Tokens:", summary_usage.total_token_count)

        print("=" * 60)

    except Exception as e:
        print("Summary token usage not available:", e)

    raw = (response.text or "").strip()
    try:
        data = parse_gemini_json(raw)
    except Exception:
        data = {}

    short_summary = data.get("short_summary", "Short summary not generated.")
    long_summary = data.get("summary", "Summary not generated.")
    return short_summary, long_summary


def analyze_audio_with_gemini(audio_path):
    print(">>> ENTERED analyze_audio_with_gemini", flush=True)
    print("====================================")
    print("START analyze_audio_with_gemini")
    print("Audio Path :", audio_path)
    print("====================================")
    total_start = time.time()
    client = get_google_client()
    keywords_list = load_keywords()
    keywords_str = ", ".join(keywords_list)
    # uploaded_file = None
    # try:
    #     #uploaded_file = client.files.upload(file=audio_path, config=types.UploadFileConfig(mime_type="audio/wav"))
    #     for attempt in range(3):
    #         try:
    #             uploaded_file = client.files.upload(
    #                 file=audio_path,
    #                 config=types.UploadFileConfig(
    #                     mime_type="audio/ogg"
    #                 )
    #             )
    #             break

    #         except Exception as e:
    #             print(f"Upload Retry {attempt+1}: {e}")

    #             if attempt == 2:
    #                 raise

    #             time.sleep(2)
    #     print(">>> FILE UPLOADED, calling transcript function now", flush=True)
    #     transcript = get_audio_transcripts_with_gemini(client, uploaded_file)
        #transcript = whisper_transcribe(audio_path)
    try:

        print(">>> STARTING LOCAL WHISPER", flush=True)

        transcript = whisper_transcribe(audio_path)
        print("========== WHISPER TRANSCRIPT ==========", flush=True)
        print(transcript, flush=True)
        print("========================================", flush=True)
        print("WHISPER TRANSCRIPT LENGTH:", len(transcript), flush=True)
        print(">>> LOCAL WHISPER TRANSCRIPTION COMPLETED", flush=True)
        
        if not transcript:
            empty_token_info = {"prompt": 0, "response": 0, "thinking": 0, "total": 0}
            return (
                "No transcript generated.",
                "",
                "No facts detected.",
                "No analysis available.",
                "Summary not generated.",
                "None detected",
                empty_token_info
            )

        response_schema = {
            "type": "object",
            "properties": {
                "executive_name": {"type": "string"},
                "greetings": {"type": "array", "items": {"type": "string"}},
                "closings": {"type": "array", "items": {"type": "string"}},
                "customer_mood": {"type": "string", "enum": ["Frustrated", "Concerned", "Satisfied", "Neutral"]},
                "mood_reason": {"type": "string"},
                "executive_tone": {
                    "type": "string",
                    "enum": [
                        "Helpful",
                        "Helpful but Defensive",
                        "Defensive",
                        "Neutral"
                    ]
                },
                "keywords": {"type": "array", "items": {"type": "string"}},
                "business_facts": {
                    "type": "object",
                    "properties": {
                        "issues": {"type": "array", "items": {"type": "string"}},
                        "requests": {"type": "array", "items": {"type": "string"}},
                        "actions": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["issues", "requests", "actions"]
                },
                "other_topics": {
                    "type": "array",
                    "items": {"type": "string"}
                }
                

            },
            "required": ["executive_name", "greetings", "closings", "customer_mood", "mood_reason", "executive_tone", "keywords", "business_facts","other_topics"]
            
        }
        import re

        # Remove timestamps + tone labels + speaker labels
        clean_transcript = re.sub(
            r"\[\d{2}:\d{2}\]|\[(Loudly|Shouting|Silently|Calmly)\]",
            "",
            transcript,
            flags=re.IGNORECASE
        )

        clean_transcript = re.sub(
            r"\b(Executive|Customer)\s*:\s*",
            "",
            clean_transcript,
            flags=re.IGNORECASE
        )
        clean_transcript = re.sub(
            r"\b(ok|okay|बरं|hmm|sir|madam)\b",
            "",
            clean_transcript,
            flags=re.IGNORECASE
        )

        # Remove repeated greetings
        clean_transcript = re.sub(
            r"(हॅलो\s*){2,}",
            " ",
            clean_transcript,
            flags=re.IGNORECASE
        )

        clean_transcript = re.sub(
            r"(hello\s*){2,}",
            " ",
            clean_transcript,
            flags=re.IGNORECASE
        )
        clean_transcript = re.sub(
            r"\s+",
            " ",
            clean_transcript
        ).strip()

        

        prompt = f"""
Analyze this customer support transcript.

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

Return valid JSON only.

Required fields:

1. executive_name:
- Scan the ENTIRE conversation from start to end carefully — the name can appear anywhere: beginning, middle, or end of the call.
- Case A: The customer (or anyone else) addresses the Executive by name at any point — in any language or script (Hindi/Marathi/English, Devanagari or Latin), with or without honorifics like "जी"/"सर"/"ji"/"sir".
  Example: "Thank you Rahul", "Ravi, can you help?", "अजीत जी हेलो", "धन्यवाद अजीत सर", Sandeep 
- Case B: The Executive states their own name at any point in the call (not just the beginning) — e.g. "मेरा नाम अजीत है", "This is Rahul speaking", "अजीत बोल रहा हूं".
- Casual, broken, or informal sentence phrasing is fine in both cases — still extract the name as long as it is clearly being used as a name (by the customer addressing the executive, OR by the executive referring to themselves).
- If a name is found under EITHER Case A or Case B anywhere in the transcript, use it as executive_name.
- Only return "Not mentioned" if NO name is spoken anywhere in the entire conversation, by either party.
- NEVER invent a name that is not explicitly present in the transcript.

2. greetings:
- Detect ONLY from lines with timestamp [00:00] to [00:15].
- Example: [00:02] Executive: "Hare Krishna" → capture "Hare Krishna"
- Ignore greetings appearing after [00:15].
- If no clear greeting phrase is found in that window, return the first spoken line (verbatim, any language) as the greeting — never return an empty array.

3. closings:
- Detect ONLY from the last 15 seconds of the call.
- Ignore mid-call conversation.
- If no clear closing phrase is found in that window, return the last spoken line (verbatim, any language) as the closing — never return an empty array.

4. customer_mood:
- "Frustrated": customer is SHOUTING, raising voice [Loudly]/[Shouting], sounds visibly annoyed/irritated,
  or uses abusive/swear words. This is ONLY about TONE, not topic — a customer calmly reporting a problem
  is NOT Frustrated.
- "Concerned": customer calmly/normally mentions that an issue is repeating, has happened before,
  or that a feature/option is missing — but WITHOUT raising voice, anger, or abusive language.
  Normal conversational tone while describing a real, recurring problem = Concerned, not Frustrated.
- "Neutral": calm tone throughout, normal question-answer flow, no repeated/recurring issue mentioned.
- "Satisfied": thanks executive, confirms issue resolved, positive closing.
- DEFAULT to "Neutral" if no clear signal exists.
- CRITICAL: Do NOT choose Frustrated just because the customer mentions a problem/issue/complaint —
  only choose Frustrated if the TONE itself (shouting, loud, angry, abusive) shows it.

5. mood_reason
6. executive_tone

    Return ONLY one of these values:

    - Helpful
    - Helpful but Defensive
    - Defensive
    - Neutral


7. keywords
    - Select a keyword ONLY if that specific topic/feature was EXPLICITLY and CLEARLY
      discussed in the conversation — the term itself, or a direct/obvious translation
      of it (Hindi/Marathi), must actually be spoken.
    - Do NOT select a keyword based on loose thematic association, general context of the
      call (e.g. "this is a billing software support call so maybe printing came up"), or
      because a related feature was mentioned. The specific topic itself must be concretely
      present in the transcript.
    - Do NOT select a keyword just because an unrelated word happens to sound similar —
      the underlying topic must actually have been discussed, in explicit terms.
    - When in doubt, DO NOT include it — it is better to return fewer keywords than wrong ones.
    - If no keyword topic was clearly and explicitly discussed, return [].


8. business_facts:
   - issues
   - requests
   - actions

9. other_topics:
   - Independently identify the MAIN issue/topic/feature discussed in this call, in 1-4 short phrases,
     regardless of whether it matches the Keywords list below.
   - Examples: "PAN card issue", "GST number update", "login problem", "invoice mismatch".
   - Use short, specific phrases (2-5 words each), not full sentences.
   - This is separate from the Keywords list — capture whatever the call was actually about.





Keywords:
{keywords_str}

Output JSON only.
"""

#         prompt = f"""
# Analyze this customer support call transcript carefully.

# Pay close attention to:
# - Speaker identity
# - Speaker consistency
# - Role ownership
# - Vocal cues in brackets like [Loudly], [Shouting], [Silently], [Calmly]

# TRANSCRIPT:
# {transcript}

# =================================================
# IMPORTANT ROLE IDENTIFICATION
# =================================================

# "Executive" =
# - Support agent
# - Company representative
# - Person providing troubleshooting
# - Person helping solve issue
# - Person giving technical guidance

# "Customer" =
# - Person calling for support
# - Person reporting issue
# - Person asking questions
# - Person requesting solution
# - Person demanding complaint number or escalation

# IMPORTANT:
# Never confuse Executive and Customer.

# Usually:

# CUSTOMER:
# - reports issue
# - repeats issue
# - explains problem
# - shares system ID/password
# - answers troubleshooting questions
# - sounds frustrated

# EXECUTIVE:
# - asks for system ID/password
# - provides troubleshooting
# - gives instructions
# - asks customer to keep machine connected
# - informs callback timing
# - provides technical guidance
# - confirms follow-up

# =================================================
# CRITICAL ROLE ACCURACY RULES
# =================================================

# - Carefully identify WHO spoke each statement.
# - NEVER reverse customer and executive actions.
# - Always use transcript evidence.
# - Never assume based on sentence structure alone.

# ABSOLUTE RULE:

# PERSON ASKING = requester.

# PERSON PROVIDING = provider.

# Examples:

# WRONG:
# "Executive requested callback after 10 minutes."

# CORRECT:
# "Customer requested callback after 10 minutes."

# WRONG:
# "Executive requested complaint number."

# CORRECT:
# "Customer requested complaint number."

# WRONG:
# "Executive requested machine remain connected."

# CORRECT:
# "Customer requested machine remain connected."

# WRONG:
# "Customer shared system ID and password."

# CORRECT:
# "Executive shared system ID and password."

# =================================================
# FIELD RULES
# =================================================

# 1. executive_name:
# - Extract executive name ONLY if explicitly spoken.
# - Never guess names.
# - If not mentioned return:
# "Not mentioned"

# 2. customer_mood:
# Analyze ONLY CUSTOMER behavior.

# Angry:
# - shouting
# - rude tone
# - aggressive behavior
# - threatening complaint
# - argumentative discussion

# Frustrated:
# - repeating issue
# - demanding fast solution
# - sounding impatient
# - asking repeatedly
# - complaint-related discussion
# - unresolved issue frustration

# Neutral:
# - calm professional discussion

# Satisfied:
# - happy with resolution

# 3. mood_reason:
# - Clearly explain WHY customer mood was selected.
# - Mention exact CUSTOMER behavior.
# - NEVER describe executive behavior here.

# 4. executive_tone:
# Analyze ONLY EXECUTIVE behavior.

# Possible values:
# - Professional
# - Helpful
# - Calm
# - Supportive
# - Patient
# - Polite
# - Defensive
# - Confused

# IMPORTANT:
# Never mix customer tone with executive tone.

# 5. keywords:
# ONLY extract keywords if mentioned exactly.

# Allowed keywords:
# - "Whatsapp"
# - "PhonePe"
# - "UPI"
# - "Mobile Reports"
# - "GSTR2A"
# - "Multibranch"
# - "Dashboard"
# - "Analytics"
# - "Purchase Order"
# - "Purchase"
# - "Barcode printing"
# - "Label designer"
# - "Bill designer"
# - "User rights"
# - "Counterwise report"
# - "Release notes"
# - "Price Protection"
# - "IMEI tracking"
# - "Bill Print"
# - "Expiry tracking"
# - "Interbranch IBT"
# - "Sync Issue"
# - "Reports issue"
# - "Printing issue"
# - "Product Scan Issue"
# - "Product used by thousands of clients"

# 6. short_summary:
# Write concise professional summary.

# Rules:
# - 2 to 3 sentences only
# - Include:
#   * why customer called
#   * main issue
#   * current status / outcome

# 7. summary:
# Write detailed LONG summary.

# Rules:
# - Minimum 10 to 15 sentences
# - Include:
#   * why customer called
#   * issue discussed
#   * customer complaints
#   * customer requests
#   * executive explanations
#   * troubleshooting provided
#   * technical guidance
#   * resolution status
#   * pending actions
#   * callback/follow-up
#   * customer reaction
#   * final outcome

# IMPORTANT:
# Do NOT make it short.

# 8. business_facts:

# Extract ONLY transcript-supported facts.

# Maintain STRICT speaker accuracy.

# Customer issues:
# - problems reported by customer

# Customer requests:
# ONLY requests made BY CUSTOMER:
# - complaint number request
# - callback request
# - urgent resolution request
# - wait request
# - machine connection request
# - system ID/password request
# - remote support request

# Executive actions:
# ONLY actions done BY EXECUTIVE:
# - troubleshooting provided
# - explanations provided
# - guidance shared
# - system ID shared
# - password shared
# - callback agreement
# - follow-up confirmation
# - escalation

# IMPORTANT:
# Split request vs action correctly.

# Example:

# CORRECT:

# Customer request:
# - Customer requested machine remain connected for 10 minutes.
# - Customer requested callback after 10 minutes.

# Executive action:
# - Executive agreed to callback after 10 minutes.

# WRONG:
# - Executive requested callback after 10 minutes.

# Minimum:
# 2 to 5 detailed points whenever possible.

# 9. analysis:
# Internally analyze:
# - customer behavior
# - executive handling
# - communication quality
# - resolution quality

# Ensure correct role mapping before finalizing.


# =================================================
# STRICT JSON RULES
# =================================================

# - Output VALID JSON only
# - NO markdown
# - NO explanation outside JSON
# - NO literal newlines inside JSON strings
# - Use \\n for line breaks
# """
        
        analysis_start = time.time()
        response = safe_generate(
            client,
            model=ANALYSIS_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig( 
                temperature=0,
                response_mime_type="application/json",
                response_schema=response_schema,
                max_output_tokens=6000,
            )
        )
        try:
            # usage = response.usage_metadata
            # prompt_tokens = getattr(usage, "prompt_token_count", 0)
            # response_tokens = getattr(usage, "candidates_token_count", 0)
            # thinking_tokens = getattr(usage, "thoughts_token_count", 0)
            usage = response.usage_metadata

            cost_info = calculate_gemini_cost(usage)

            prompt_tokens = cost_info["input_tokens"]
            response_tokens = cost_info["output_tokens"]
            thinking_tokens = cost_info["thinking_tokens"]

            print("\n" + "=" * 60)
            print("GEMINI ANALYSIS COST")
            print("=" * 60)

            print("Input Tokens    :", prompt_tokens)
            print("Output Tokens   :", response_tokens)
            print("Thinking Tokens :", thinking_tokens)
            print("Total Tokens    :", cost_info["total_tokens"])
            print("Cost USD        : $%.6f" % cost_info["cost_usd"])
            print("Cost INR        : ₹%.4f" % cost_info["cost_inr"])

            print("=" * 60)
            print("Thinking Tokens:", thinking_tokens) 
            total_tokens = prompt_tokens + response_tokens

            print("Prompt Tokens:", prompt_tokens)
            print("Response Tokens:", response_tokens)
            print("Actual Total Tokens:", total_tokens)

            if hasattr(usage, "total_token_count"):
                print("SDK Total Tokens:", usage.total_token_count)
            try:                                                             
                finish_reason = response.candidates[0].finish_reason         
                print("FINISH REASON:", finish_reason)                       
            except Exception as fe:                                          
                print("Could not get finish_reason:", fe)     

        except Exception as e:
            print("Token usage not available:", e)


        
        raw_output = (response.text or "").strip()
        print(f"[TIMER] Analysis (Gemini) took {time.time() - analysis_start:.2f} seconds")
        save_text(raw_output, audio_path, "_gemini_raw_response.txt")
        try:
            print("\nRAW GEMINI OUTPUT:\n")
            print(raw_output)
            print("\nEND OUTPUT\n")
        except UnicodeEncodeError:
            print("\nRAW GEMINI OUTPUT: (contains non-ASCII characters, skipped)\n")

        print("HAS SHORT SUMMARY:", '"short_summary"' in raw_output)
        print("HAS LONG SUMMARY:", '"summary"' in raw_output)
        data = robust_parse(raw_output)
        print("RAW EXECUTIVE TONE:", repr(data.get("executive_tone")))
        tone = data.get("executive_tone", "").lower()

        if "defensive" in tone:
            data["executive_tone"] = "Helpful but Defensive"

        elif tone:
            data["executive_tone"] = "Helpful"

        else:
            data["executive_tone"] = "Neutral"
        # Sanity: default to Neutral if no strong signals
        # Sanity check: only override if mood contradicts transcript evidence
        

        # TONE-based anger signals ONLY (shouting/loud/abusive) — these are
        # the ONLY things that should ever produce "Frustrated".
        mood = data.get("customer_mood", "Neutral")
        transcript_lower = transcript.lower()
        customer_text_lower = extract_speaker_text(transcript, "Customer")

        # TONE-based anger signals ONLY (shouting/abusive) — "[loudly]"
        # deliberately EXCLUDED: loud/insistent speech (e.g. customer
        # confirming loudly so they're heard, or repeating a problem
        # urgently) is NOT the same as anger, and was causing false
        # "Frustrated" results.
        anger_tone_signals = [
            "[shouting]",
            "चिडलो", "चिडले", "गुस्सा", "गुस्से में",
            "irritated", "angry", "annoyed", "fed up",
            # Add specific abusive/swear words used in your calls here:
            "bakwas", "बकवास", "नालायक", "नालायक आहेस",
        ]

        # TOPIC signals — customer describing a recurring problem, but in a
        # normal tone. These should map to "Concerned", never "Frustrated".
        recurring_issue_signals = [
            "again", "every time", "always", "still not", "not working",
            "2 months", "3 months", "again and again", "complaint number",
            "काम नाही", "परत", "नाही होत", "बंद आहे",
            "नाही चालत", "होत नाही", "का नाही",
            "क्यों नहीं", "नहीं हो रहा", "नहीं आ रहा",
            "kab tak", "kitne din", "bahut time",
            "feature नाहीये", "option नाहीये", "missing",
        ]

        # Check anger ONLY within the Customer's own dialogue (not the
        # Executive's, not [Aside] background chatter) — otherwise the
        # Executive's tone or an unrelated remark could wrongly flag the
        # customer as angry.
        # has_anger_tone = any(s in customer_text_lower for s in anger_tone_signals)
        # has_recurring_issue = any(s in customer_text_lower for s in recurring_issue_signals)

        # if has_anger_tone:
        #     # Tone evidence present -> Frustrated, no matter what the topic is.
        #     data["customer_mood"] = "Frustrated"
        #     data["mood_reason"] = "Customer's tone showed shouting/anger/abusive language during the call."
        # elif mood == "Frustrated" and not has_anger_tone:
        #     # Model said Frustrated but no real tone evidence -> downgrade.
        #     if has_recurring_issue:
        #         data["customer_mood"] = "Concerned"
        #         data["mood_reason"] = "Customer calmly reported a recurring issue or missing feature, without anger."
        #     else:
        #         data["customer_mood"] = "Neutral"
        #         data["mood_reason"] = "Customer tone was calm throughout the call."
        # elif mood == "Concerned" and not has_recurring_issue:
        #     # Model said Concerned but transcript doesn't support a recurring issue -> Neutral.
        #     data["customer_mood"] = "Neutral"
        #     data["mood_reason"] = "No recurring issue or complaint detected in transcript."
        MIN_CUSTOMER_CHARS = 15  # need this much Customer-labeled text to trust the mood check

        if len(customer_text_lower.strip()) < MIN_CUSTOMER_CHARS:
            # Speaker separation didn't give us enough Customer-labeled lines,
            # so we genuinely can't judge mood — say so clearly instead of
            # quietly defaulting to "Neutral".
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

    #     VALID_CLOSINGS = {
    #         "ok", "okay", "ठीक आहे",
    #         "ओके", "thank you",
    #         "thanks", "bye"
    #     }

    #     HONORIFICS = {"sir", "madam", "सर", "मॅडम"}

    #     clean_greetings = []
    #     for g in data.get("greetings", []):
    #         g_lower = g.lower().strip()
    #         words = g_lower.split()

    #         if len(words) > 4:
    #             continue

    
    #         core = " ".join(w for w in words if w not in HONORIFICS).strip()

    #         if core in VALID_GREETINGS:
    #             clean_greetings.append(core)        # "hare krishna" ✅
    #         elif words[0] in VALID_GREETINGS:
    #             clean_greetings.append(words[0])    # "hello sir" → "hello" ✅

    #     data["greetings"] = list(dict.fromkeys(clean_greetings))[:3]

    #     clean_closings = []

    #     for c in data.get("closings", []):
    #         c_lower = c.lower().strip()

    #         words = c_lower.split()

    # # ignore long sentences in closing
    #         if len(words) > 5:
    #             continue

    #         if (
    #             c_lower in VALID_CLOSINGS or
    #             any(c_lower.startswith(x) for x in VALID_CLOSINGS)
    #         ):
    #             clean_closings.append(c.strip())

        
    #     data["closings"] = list(dict.fromkeys(clean_closings))[:3]
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
            """Scan the word list and PICK OUT any known greeting phrases
            wherever they appear (e.g. ['hello','good','morning','mam',
            'retailware','se','ginna','bolte'] -> ['hello', 'good morning']).
            Unrecognized words (names, honorifics, sentence continuation)
            are simply skipped instead of discarding the whole line."""
            matched = []
            i, n = 0, len(words)
            while i < n:
                matched_here = False
                for phrase in GREETING_PHRASE_TUPLES:
                    plen = len(phrase)
                    if plen and tuple(words[i:i+plen]) == phrase:
                        matched.append(" ".join(phrase))
                        i += plen
                        matched_here = True
                        break
                if not matched_here:
                    i += 1  # skip this unrecognized word, keep scanning
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
            # No valid greeting phrase matched — default to "Hello" instead
            # of falling back to a raw, unfiltered spoken line.
            clean_greetings = ["Hello"]

        data["greetings"] = list(dict.fromkeys(clean_greetings))[:5]

        clean_closings = []

        for c in data.get("closings", []):
            c_lower = _clean_word(c.lower().strip())

            words = c_lower.split()

    # ignore long sentences in closing
            if len(words) > 5 or not words:
                continue

            if (
                c_lower in VALID_CLOSINGS or
                any(c_lower.startswith(x) for x in VALID_CLOSINGS)
            ):
                clean_closings.append(c_lower)

        data["closings"] = list(dict.fromkeys(clean_closings))[:3]

        import difflib

        transcript_lower = transcript.lower()
        transcript_words = set(re.split(r'\W+', transcript_lower))

        def keyword_match(kw, max_gap_chars=30):
            """A multi-word keyword only matches if ALL its words appear close
            together (within max_gap_chars characters of each other, any order) —
            not just anywhere, independently, in the whole transcript."""
            from itertools import permutations

            kw_lower = kw.lower().strip()
            if kw_lower in transcript_lower:
                return True

            words = [w for w in re.split(r'\W+', kw_lower) if len(w) > 2]
            if len(words) < 2:
                return False

            for perm in permutations(words):
                pattern = (r'\b.{0,' + str(max_gap_chars) + r'}\b').join(
                    re.escape(w) for w in perm
                )
                if re.search(pattern, transcript_lower, re.DOTALL):
                    return True

            return False

        detected_keywords = [kw for kw in keywords_list if keyword_match(kw)]

        

        # bring in Gemini's own semantic picks too, but only trust ones
        # that actually exist in the master keyword list (no hallucinated labels)
        keywords_list_lower = {k.lower(): k for k in keywords_list}
        model_keywords = [
            keywords_list_lower[k.lower()]
            for k in data.get("keywords", [])
            if k.lower() in keywords_list_lower
            and keyword_match(keywords_list_lower[k.lower()])   # verify transcript actually supports it
        ]

        final_keywords = list(dict.fromkeys(model_keywords + detected_keywords))

        if not final_keywords:
            final_keywords = ["No matching keyword detected"]

        data["keywords"] = final_keywords

        facts = data.get("business_facts", {})

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


    # correct: executive asked for credentials
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
            # No known name matched — verify Gemini's own extraction actually
            # appears in the transcript before trusting it (blocks hallucination)
            name = data.get("executive_name", "Not mentioned")
            if name and name.lower() not in ["not mentioned", "not detected", "n/a"]:
                if name.lower() in transcript.lower():
                    data["executive_name"] = name
                else:
                    data["executive_name"] = "Not mentioned"
            else:
                data["executive_name"] = "Not mentioned"

        def to_s(v): return ", ".join(v) if isinstance(v, list) and v else str(v or "None detected")

        customer_mood = data.get("customer_mood", "Neutral")
        mood_reason = data.get("mood_reason", "N/A")
        executive_tone = data.get("executive_tone", "Normal")

        # analysis_text = f"Customer Mood: {data.get('customer_mood', 'Neutral')} ({data.get('mood_reason', 'N/A')})\n"
        # analysis_text += f"Executive Name: {data.get('executive_name', 'Not mentioned')}\n"
        # analysis_text += f"Greetings: {to_s(data.get('greetings'))}\n"
        # analysis_text += f"Closings: {to_s(data.get('closings'))}\n"
        # analysis_text += f"Executive Tone: {data.get('executive_tone', 'Normal')}"

        analysis_text = f"Customer Mood: {customer_mood} ({mood_reason})\n"
        analysis_text += f"Executive Name: {data.get('executive_name', 'Not mentioned')}\n"
        analysis_text += f"Greetings: {to_s(data.get('greetings'))}\n"
        analysis_text += f"Closings: {to_s(data.get('closings'))}\n"
        analysis_text += f"Executive Tone: {executive_tone}"
        
        short_summary, long_summary = generate_detailed_summary(
            client,
            clean_transcript,
            data.get("business_facts", {}),
            data.get("executive_name", "Not mentioned"),
            customer_mood,
            mood_reason
        )

        combined_summary = (
            "SHORT SUMMARY\n"
            + "="*50
            + "\n"
            + short_summary
            + "\n\nLONG SUMMARY\n"
            + "="*50
            + "\n"
            + long_summary
        )
        print(f"[TIMER] TOTAL analyze_audio_with_gemini took {time.time() - total_start:.2f} seconds")
        token_info = {
            "prompt": prompt_tokens,
            "response": response_tokens,
            "thinking": thinking_tokens,
            "total": total_tokens
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
        return parse_gemini_json(raw)
    except:
        try:
            return json.loads(clean_json_string(raw))
        except:
            import re
            def get_s(k):
                return (
                    re.findall(rf'"{k}"\s*:\s*"(.*?)"', raw, re.DOTALL)
                    or ["Not detected"]
                )[0]

            def get_a(k):
                m = re.search(rf'"{k}"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
                if m: return [x.strip().strip('"').strip("'") for x in m.group(1).split(",") if x.strip()]
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
    with open(os.path.splitext(audio_path)[0] + "_transcript.txt", "w", encoding="utf-8") as f: f.write(transcript)

def save_text(text, audio_path, suffix):
    with open(os.path.splitext(audio_path)[0] + suffix, "w", encoding="utf-8") as f: f.write(text)

def save_summary(summary, analysis, facts, keywords, audio_path):
    with open(os.path.splitext(audio_path)[0] + "_summary.txt", "w", encoding="utf-8") as f:
        f.write("="*50 + "\nANALYSIS\n" + "="*50 + f"\n\n{analysis}\n\n" + "="*50 + "\nKEYWORDS IDENTIFIED\n" + "="*50 + f"\n\n{keywords}\n\n" + "="*50 + "\nBUSINESS FACTS\n" + "="*50 + f"\n\n{facts}\n\n" + "="*50 + "\nSUMMARY\n" + "="*50 + f"\n\n{summary}")