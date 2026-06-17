from google import genai
from google.genai import types
tk = None
import threading
import subprocess
import os
import time
import json
import sys


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
TRANSCRIPT_MODEL = "models/gemini-2.5-flash"
ANALYSIS_MODEL = "models/gemini-2.5-flash-lite"
SUMMARY_SEED = 12345

# -------- BACKEND FUNCTIONS --------

def get_google_client():
    if not GOOGLE_API_KEY:
        raise RuntimeError("Set GOOGLE_API_KEY in your .env file before running this app.")
    return genai.Client(api_key=GOOGLE_API_KEY)
def safe_generate(client, **kwargs):
    max_retries = 3

    for attempt in range(max_retries):
        try:
            return client.models.generate_content(**kwargs)

        except Exception as e:
            err = str(e).lower()

            print(f"Retry {attempt+1}: {e}")

            # retry only network/server issues
            retryable = any(x in err for x in [
                "server disconnected",
                "timeout",
                "503",
                "connection",
                "429",
                "internal"
            ])

            if not retryable:
                raise

            if attempt == max_retries - 1:
                raise

            time.sleep(2)
def convert_audio(input_path):
    output_path = os.path.splitext(input_path)[0] + "_16k.wav"
    result = subprocess.run([
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        output_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[:300]}")
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

def get_audio_transcripts_with_gemini(client, uploaded_file):
    prompt = (
        "Transcribe this support call exactly. Add [MM:SS] timestamps. "
        "Identify speakers as 'Executive' and 'Customer'. "
        "IMPORTANT: In brackets, note the tone or volume if it changes significantly "
        "(e.g., [Loudly], [Shouting], [Silently], [Calmly]). "
        "Keep the original language (Hindi/Marathi/English) as spoken. "
        "Use Devanagari script for Hindi/Marathi and Latin script for English."
    )
    
    response = safe_generate(
        client,
        model=TRANSCRIPT_MODEL,
        contents=[uploaded_file, prompt],
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=8000
        )
    )
    return (response.text or "").strip()

def load_keywords(file_path="keywords.txt"):
    if not os.path.exists(file_path):
        return [
            "Whatsapp", "PhonePe", "UPI", "Mobile Reports", "GSTR2A",
            "Multibranch", "Dashboard", "Analytics", "Purchase Order",
            "Purchase", "Barcode printing", "Label designer",
            "Bill designer", "User rights", "Counterwise report",
            "Release notes", "Price Protection", "IMEI tracking",
            "Bill Print", "Expiry tracking", "Interbranch IBT",
            "Sync Issue", "Reports issue", "Printing issue",
            "Product Scan Issue", "Bill Modification or edit", "Incentive", "loss",
            "scheme"
        ]
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def analyze_audio_with_gemini(audio_path):
    client = get_google_client()
    keywords_list = load_keywords()
    keywords_str = ", ".join(keywords_list)
    uploaded_file = None
    try:
        #uploaded_file = client.files.upload(file=audio_path, config=types.UploadFileConfig(mime_type="audio/wav"))
        for attempt in range(3):
            try:
                uploaded_file = client.files.upload(
                    file=audio_path,
                    config=types.UploadFileConfig(
                        mime_type="audio/wav"
                    )
                )
                break

            except Exception as e:
                print(f"Upload Retry {attempt+1}: {e}")

                if attempt == 2:
                    raise

                time.sleep(2)
        transcript = get_audio_transcripts_with_gemini(client, uploaded_file)
        
        if not transcript:
            return "No transcript generated.", "", "No facts detected.", "No analysis available.", "Summary not generated."

        response_schema = {
            "type": "object",
            "properties": {
                "executive_name": {"type": "string"},
                "greetings": {"type": "array", "items": {"type": "string"}},
                "closings": {"type": "array", "items": {"type": "string"}},
                "customer_mood": {"type": "string", "enum": ["Angry", "Frustrated", "Satisfied", "Neutral"]},
                "mood_reason": {"type": "string"},
                "executive_tone": {"type": "string"},
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
                "short_summary": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["executive_name", "greetings", "closings", "customer_mood", "mood_reason", "executive_tone", "keywords", "business_facts","short_summary", "summary"]
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
{transcript}


Rules:
- Executive = support person
- Customer = person reporting issue
- Customer usually reports issue and shares system ID/password.
- Executive usually asks for system ID/password, gives troubleshooting, callback timing, and technical guidance.
- Never reverse customer and executive actions.

Return valid JSON only.

Required fields:

1. executive_name

2. greetings
- Detect ONLY from lines with timestamp [00:00] to [00:15].
- Example: [00:02] Executive: "Hare Krishna" → capture "Hare Krishna"
- Ignore greetings appearing after [00:15].

3. closings
- Detect ONLY from the last 15 seconds of the call.
- Ignore mid-call conversation.

4. customer_mood
5. mood_reason
6. executive_tone
7. keywords
    - Detect closest matching keyword from transcript.
    - Example:
        print, printing, bill printing, print slow → Bill Print
        purchase data, purchase return, purchase cancellation → Purchase
        rights issue, access issue → User rights
    - Return ONLY keywords from allowed keyword list.
    - If no keyword found return [].   
8. business_facts:
   - issues
   - requests
   - actions
9. short_summary:
   - 2-3 sentences
10. summary:
   - detailed summary (4-6 sentences)



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
        
        
        response = safe_generate(
            client,
            model=ANALYSIS_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=response_schema,
                max_output_tokens=2500
            )
        )
        try:
            usage = response.usage_metadata
            prompt_tokens = getattr(usage, "prompt_token_count", 0)
            response_tokens = getattr(usage, "candidates_token_count", 0)
            total_tokens = prompt_tokens + response_tokens

            print("Prompt Tokens:", prompt_tokens)
            print("Response Tokens:", response_tokens)
            print("Actual Total Tokens:", total_tokens)

            if hasattr(usage, "total_token_count"):
                print("SDK Total Tokens:", usage.total_token_count)
            

        except Exception as e:
            print("Token usage not available:", e)


        
        raw_output = (response.text or "").strip()
        save_text(raw_output, audio_path, "_gemini_raw_response.txt")
        try:
            print("\nRAW GEMINI OUTPUT:\n")
            print(raw_output)
            print("\nEND OUTPUT\n")
        except UnicodeEncodeError:
            print("\nRAW GEMINI OUTPUT: (contains non-ASCII characters, skipped)\n")
        
        data = robust_parse(raw_output)
        # -------- GREETINGS / CLOSINGS SANITY FILTER --------
        VALID_GREETINGS = {
            "hello", "hi", "good morning",
            "good afternoon", "good evening",
            "namaste", "नमस्ते",
            "हॅलो", "हेलो",
            "hare krishna", "हरे कृष्ण", "हरे कृष्णा",
            "jai shree krishna", "जय श्री कृष्ण",
            "jai hind",
            "radhe radhe"

        }

        VALID_CLOSINGS = {
            "ok", "okay", "ठीक आहे",
            "ओके", "thank you",
            "thanks", "bye"
        }

        HONORIFICS = {"sir", "madam", "सर", "मॅडम"}

        clean_greetings = []
        for g in data.get("greetings", []):
            g_lower = g.lower().strip()
            words = g_lower.split()

            if len(words) > 4:
                continue

    
            core = " ".join(w for w in words if w not in HONORIFICS).strip()

            if core in VALID_GREETINGS:
                clean_greetings.append(core)        # "hare krishna" ✅
            elif words[0] in VALID_GREETINGS:
                clean_greetings.append(words[0])    # "hello sir" → "hello" ✅

        data["greetings"] = list(dict.fromkeys(clean_greetings))[:3]

        clean_closings = []

        for c in data.get("closings", []):
            c_lower = c.lower().strip()

            words = c_lower.split()

    # ignore long sentences in closing
            if len(words) > 5:
                continue

            if (
                c_lower in VALID_CLOSINGS or
                any(c_lower.startswith(x) for x in VALID_CLOSINGS)
            ):
                clean_closings.append(c.strip())

        
        data["closings"] = list(dict.fromkeys(clean_closings))[:3]


        transcript_lower = transcript.lower()
        detected_keywords = []

        for kw in keywords_list:
            if kw.lower() in transcript_lower:
                detected_keywords.append(kw)

        # Add aliases for robustness
        if "print" in transcript_lower and "Bill Print" in keywords_list:
            detected_keywords.append("Bill Print")
        if "bill modify" in transcript_lower and "Bill Modification or edit" in keywords_list:
            detected_keywords.append("Bill Modification or edit")

        #data["keywords"] = list(dict.fromkeys(detected_keywords))
        model_keywords = data.get("keywords", [])

        final_keywords = (
            model_keywords +
            detected_keywords
        )

        data["keywords"] = list(
            dict.fromkeys(final_keywords)
        )
            # -------- ROLE SANITY FIX --------

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

    # wrong: executive shared credentials
            if any(x in text for x in [
                "shared system id",
                "shared password",
                "shared the system id",
                "system id and password"
            ]):
                fixed_requests.append(
                    "Customer shared system ID and password for troubleshooting."
                )
                continue

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
        
        # --- CRITICAL: ANTI-HALLUCINATION VERIFICATION ---
        name = data.get("executive_name", "Not mentioned")
        if name and name.lower() not in ["not mentioned", "not detected", "n/a"]:
            # Check if the name exists in the transcript text
            if name.lower() not in transcript.lower():
                data["executive_name"] = "Not mentioned"

        def to_s(v): return ", ".join(v) if isinstance(v, list) and v else str(v or "None detected")

        analysis_text = f"Customer Mood: {data.get('customer_mood', 'Neutral')} ({data.get('mood_reason', 'N/A')})\n"
        analysis_text += f"Executive Name: {data.get('executive_name', 'Not mentioned')}\n"
        analysis_text += f"Greetings: {to_s(data.get('greetings'))}\n"
        analysis_text += f"Closings: {to_s(data.get('closings'))}\n"
        analysis_text += f"Executive Tone: {data.get('executive_tone', 'Normal')}"

        short_summary = data.get("short_summary", "Short summary not generated.")
        long_summary = data.get("summary", "Summary not generated.")

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

        return (
            transcript,
            raw_output,
            format_facts(data.get("business_facts")),
            analysis_text,
            combined_summary,
            to_s(data.get("keywords"))
        )
    finally:
        if uploaded_file:
            try: client.files.delete(name=uploaded_file.name)
            except: pass

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

