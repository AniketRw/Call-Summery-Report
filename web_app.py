import db 
print("=== APP STARTED ===")

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import cgi
import html
import os
import time 
import re
import traceback
import threading
import socket
from urllib.parse import parse_qs, urlparse, unquote
import urllib.request
from weasyprint import HTML
import io

import subprocess
import tempfile
import shutil


# Import the backend logic from testnew
import testnew

print("=== TESTNEW IMPORTED ===")
HOST = ""
PORT = int(os.getenv("PORT", "8080"))
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "web_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)




def safe_filename(filename):
    filename = Path(filename or "audio").name
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return filename or "audio"

# Replace the reportlab imports at the top of web_app.py with:
#
# from weasyprint import HTML
#
# (Remove these old reportlab imports:)
#   from reportlab.lib.pagesizes import A4
#   from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
#   from reportlab.lib.units import mm
#   from reportlab.lib import colors
#   from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Preformatted
#   from reportlab.lib.enums import TA_LEFT, TA_CENTER
#   from reportlab.platypus import Table, TableStyle
#   from reportlab.pdfbase import pdfmetrics
#   from reportlab.pdfbase.ttfonts import TTFont
#
# Then replace register_fonts() and generate_pdf() with the code below.

def strip_speaker_labels(transcript):
    """Remove 'Executive:'/'Customer:' labels (with optional [Aside] tag) for display only."""
    lines = (transcript or "").splitlines()
    out = []
    for line in lines:
        m = re.match(
            r"(\[\s*\d{2}:\d{2}\s*\])\s*(?:Executive|Customer)\s*(?:\[[^\]]*\]\s*)?:\s*(.*)",
            line
        )
        if m:
            out.append(f"{m.group(1)} {m.group(2)}".strip())
        else:
            out.append(line)
    return "\n".join(out)


def generate_pdf(analysis, keywords, facts, summary, transcript, filename="call_summary"):
    font_path = (BASE_DIR / "fonts" / "NotoSansDevanagari.ttf").as_uri()

    def esc(t):
        return html.escape(t or "").replace("\n", "<br>")

    html_doc = f"""<!doctype html>
<html lang="mr">
<head>
<meta charset="utf-8">
<style>
  @font-face {{
    font-family: 'Devanagari';
    src: url('{font_path}');
  }}
  body {{
    font-family: 'Devanagari', 'Noto Sans Devanagari', sans-serif;
    color: #1a1a1a;
    margin: 0;
    font-size: 12px;
    line-height: 1.7;
  }}
  .header {{
    background: #0f1117;
    color: #fff;
    padding: 20px 24px;
  }}
  .header h1 {{
    margin: 0;
    font-size: 22px;
  }}
  .header p {{
    margin: 4px 0 0;
    font-size: 12px;
    color: #c7ffe8;
  }}
  .disclaimer {{
    text-align: center;
    font-size: 11px;
    color: #666;
    padding: 10px 24px;
  }}
  .section-title {{
    background: #0f9d68;
    color: #fff;
    padding: 8px 14px;
    font-size: 14px;
    font-weight: bold;
    margin-top: 14px;
  }}
  .section-body {{
    border: 1px solid #dcdcdc;
    padding: 10px 14px;
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-wrap: break-word;
  }}
  .keywords {{
    color: #0b7a4f;
    font-weight: bold;
  }}
  @page {{
    size: A4;
    margin: 15mm;
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>Call Summary Report</h1>
    <p>File: {esc(filename)}</p>
  </div>
  <p class="disclaimer">This summary is AI-generated. Please verify critical business information before use.</p>

  <div class="section-title">ANALYSIS</div>
  <div class="section-body">{esc(analysis)}</div>

  <div class="section-title">KEYWORDS IDENTIFIED</div>
  <div class="section-body keywords">{esc(keywords)}</div>

  <div class="section-title">CALL DETAILS</div>
  <div class="section-body">{esc(facts)}</div>

  <div class="section-title">SUMMARY</div>
  <div class="section-body">{esc(summary)}</div>

  <div class="section-title">TRANSCRIPT</div>
  <div class="section-body">{esc(transcript)}</div>
</body>
</html>"""

    pdf_bytes = HTML(string=html_doc, base_url=str(BASE_DIR)).write_pdf()
    return pdf_bytes

def format_duration(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "N/A"
    if seconds < 0:
        return "N/A"
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    if minutes > 0:
        return f"{minutes}m {secs:04.1f}s"
    return f"{secs:.1f}s"

def render_page(analysis="", keywords="", facts="", summary="", transcript="", error="", filename="" ,  token_info=None,elapsed_time=None):
    escaped_filename = html.escape(filename)
    
    results_html = ""
    token_html = ""
    timing_html = ""
    if token_info:
      token_html = f"""
      <div class="token-badge" id="tokenBadge">
          🪙 Gemini Tokens Used : <b>{token_info['total']}</b>
      </div>
      """
      timing_html = ""
      if elapsed_time is not None:
        timing_html = f"""
        <div class="token-badge timing-badge" id="timingBadge">
            ⏱ Processing Time : <b>{html.escape(format_duration(elapsed_time))}</b>
        </div>
        """
      
    if any([analysis, keywords, facts, summary, transcript]):
        results_html = f"""
        <section class="panel" id="resultsPanel">
          <p class="meta">File: {escaped_filename}</p>
          
          <h2 class="section-title analysis">ANALYSIS</h2>
          <pre>{html.escape(analysis)}</pre>
          
          <h2 class="section-title keywords">KEYWORDS IDENTIFIED</h2>
          <pre class="highlight">{html.escape(keywords)}</pre>
          
          <h2 class="section-title facts">CALL DETAILS</h2>
          <pre>{html.escape(facts)}</pre>
          
          <h2 class="section-title summary">SUMMARY</h2>
          <pre>{html.escape(summary)}</pre>
          
          <h2 class="section-title transcript">TRANSCRIPT</h2>
          <pre>{html.escape(transcript)}</pre>
          
          <form method="post" action="/export_pdf" style="margin-top:20px; text-align:center;">
            <input type="hidden" name="analysis" value="{html.escape(analysis)}">
            <input type="hidden" name="keywords" value="{html.escape(keywords)}">
            <input type="hidden" name="facts" value="{html.escape(facts)}">
            <input type="hidden" name="summary" value="{html.escape(summary)}">
            <input type="hidden" name="transcript" value="{html.escape(transcript)}">
            <input type="hidden" name="filename" value="{html.escape(filename)}">
            <button type="submit" style="background:var(--warning); color:#1e293b;">⬇ Export PDF</button>
          </form>
        </section>
        """

    error_html = ""
    if error:
        error_html = f'<section class="panel"><h2>Error</h2><div class="error">{html.escape(error)}</div></section>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Call Summary Tool</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    .token-badge{{
      display:inline-block;
      margin-bottom:18px;
      padding:8px 14px;
      border:1px solid var(--border);
      border-radius:6px;
      background:#ffffff;
      color:var(--accent);
      font-weight:bold;
      font-size:15px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
      .timing-badge{{color:#b8790a; }}
    }}
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --panel: #ffffff;
      --text: #1e293b;
      --muted: #64748b;
      --border: #d7dee7;
      --accent: #1c3f94;
      --accent-dark: #14306f;
      --danger: #dc2626;
      --warning: #e8a512;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    main {{
      width: min(980px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      text-align: center;
      margin-bottom: 30px;
      padding: 40px 32px 32px;
      background: var(--panel);
      border-radius: 20px;
      box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 8px 24px rgba(15,23,42,0.06);
    }}
    .hero-badges {{
      display: flex;
      gap: 10px;
      justify-content: center;
      margin-top: 20px;
      flex-wrap: wrap;
    }}
    .badge {{
      background: #eef2fb;
      color: var(--accent);
      font-size: 12.5px;
      font-weight: 600;
      padding: 6px 14px;
      border-radius: 999px;
      border: 1px solid #dbe4fb;
    }}
    h1 {{
      margin: 0;
      font-size: 30px;
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), #4f7fe0, var(--accent));
      background-size: 200% auto;
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      letter-spacing: -0.5px;
      animation: shineText 4s ease-in-out infinite;
    }}
    @keyframes shineText {{
      0%, 100% {{ background-position: 0% 50%; }}
      50% {{ background-position: 100% 50%; }}
    }}
    .phone-icon {{
      display: inline-block;
      animation: ringPhone 2.5s ease-in-out infinite;
    }}
    @keyframes ringPhone {{
      0%, 100% {{ transform: rotate(0deg); }}
      10% {{ transform: rotate(-15deg); }}
      20% {{ transform: rotate(12deg); }}
      30% {{ transform: rotate(-10deg); }}
      40% {{ transform: rotate(8deg); }}
      50% {{ transform: rotate(0deg); }}
    }}
    .sub {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 15px;
      font-weight: 500;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 28px;
      margin-bottom: 24px;
      box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 8px 24px rgba(15,23,42,0.06);
    }}
    form {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: stretch;
    }}
    .dropzone {{
      position: relative;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 18px 20px;
      border: 2px dashed var(--border);
      border-radius: 10px;
      background: #f8fafc;
      cursor: pointer;
      transition: border-color 0.2s ease, background 0.2s ease;
    }}
    .dropzone:hover, .dropzone.dragover {{
      border-color: var(--accent);
      background: #eef2fb;
    }}
    .dropzone .dz-icon {{
      font-size: 22px;
      flex-shrink: 0;
    }}
    .dropzone .dz-text {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      overflow: hidden;
    }}
    .dropzone .dz-title {{
      font-weight: 600;
      font-size: 14.5px;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .dropzone .dz-sub {{
      font-size: 12.5px;
      color: var(--muted);
    }}
    input[type="file"] {{
      position: absolute;
      inset: 0;
      opacity: 0;
      cursor: pointer;
      width: 100%;
      height: 100%;
    }}
   button, .btn {{
      position: relative;
      overflow: hidden;
      min-height: 45px;
      border: 0;
      border-radius: 8px;
      padding: 0 28px;
      background: linear-gradient(135deg, var(--accent), var(--accent-dark));
      background-size: 200% 200%;
      background-position: 0% 50%;
      color: #ffffff;
      font-weight: 700;
      letter-spacing: 0.3px;
      cursor: pointer;
      font-family: inherit;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.25s cubic-bezier(.34,1.56,.64,1),
                  box-shadow 0.25s ease,
                  background-position 0.5s ease;
      box-shadow: 0 2px 6px rgba(28,63,148,0.2);
    }}
    button::before, .btn::before {{
      content: "";
      position: absolute;
      top: 0;
      left: -75%;
      width: 50%;
      height: 100%;
      background: linear-gradient(120deg, transparent, rgba(255,255,255,0.35), transparent);
      transform: skewX(-20deg);
      transition: left 0.6s ease;
    }}
    button:hover, .btn:hover {{
      background-position: 100% 50%;
      transform: translateY(-4px) scale(1.06);
      box-shadow: 0 14px 28px rgba(28,63,148,0.4), 0 0 0 3px rgba(28,63,148,0.15);
    }}
    button:hover::before, .btn:hover::before {{
      left: 125%;
    }}
    button:active, .btn:active {{
      transform: translateY(-1px) scale(1.02);
      box-shadow: 0 4px 10px rgba(28,63,148,0.3);
    }}
    .btn-secondary {{
        background: #ffffff;
        color: var(--accent);
        border: 1.5px solid var(--accent);
        margin-top: 20px;
        box-shadow: none;
    }}
    .btn-secondary {{
        background: #ffffff;
        color: var(--accent);
        border: 1.5px solid var(--accent);
        margin-top: 20px;
        box-shadow: none;
    }}
    .btn-secondary:hover {{
        background: var(--accent);
        color: #ffffff;
        transform: translateY(-2px);
        box-shadow: 0 8px 18px rgba(28,63,148,0.25);
    }}
    
    .error {{
      color: var(--danger);
      white-space: pre-wrap;
      line-height: 1.45;
    }}
    .meta {{
      margin: 0 0 15px;
      color: var(--muted);
      font-size: 13px;
    }}
    .disclaimer {{
      text-align: center;
      font-size: 12px;
      color: var(--muted);
      margin: -10px 0 20px;
      line-height: 1.6;
    }}
    .section-title {{
      margin: 20px 0 8px;
      font-size: 14px;
      font-weight: bold;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border);
    }}
    .analysis {{ color: var(--muted); }}
    .keywords {{ color: var(--accent); }}
    .facts {{ color: var(--muted); }}
    .summary {{ color: var(--muted); }}
    .transcript {{ color: var(--muted); }}
    
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-wrap: break-word;
      line-height: 1.6;
      font-size: 13.5px;
      font-family: 'JetBrains Mono', 'Consolas', monospace;
      background: #f5f7fa;
      color: var(--text);
      padding: 16px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }}
    .highlight {{
        color: var(--accent);
        font-weight: bold;
    }}
    .progress-panel {{
      display: none;
      text-align: center;
    }}
    .progress-panel.active {{
      display: block;
    }}
    .progress-panel.active {{
      animation: fadeInUp 0.4s ease;
    }}
    @keyframes fadeInUp {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .progress-track {{
      position: relative;
      width: 100%;
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #e2e8f0;
      margin: 18px 0;
      box-shadow: inset 0 1px 3px rgba(15,23,42,0.08);
    }}
    .progress-bar {{
      position: relative;
      width: 0%;
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), #4f7fe0, var(--accent));
      background-size: 200% 100%;
      animation: shimmerMove 1.6s linear infinite;
      transition: width 500ms ease;
      box-shadow: 0 0 12px rgba(28,63,148,0.5);
    }}
    @keyframes shimmerMove {{
      0% {{ background-position: 0% 50%; }}
      100% {{ background-position: 200% 50%; }}
    }}
    .progress-bar::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.5), transparent);
      width: 40%;
      animation: sweepMove 1.2s ease-in-out infinite;
    }}
    @keyframes sweepMove {{
      0% {{ transform: translateX(-100%); }}
      100% {{ transform: translateX(350%); }}
    }}
    .progress-text {{
      margin: 0;
      color: var(--accent);
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
    }}
    .progress-text .spinner {{
      width: 16px;
      height: 16px;
      border: 2.5px solid rgba(28,63,148,0.2);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      flex-shrink: 0;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .progress-percent {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 500;
      margin-top: 6px;
    }}
    .busy button {{
      opacity: 0.6;
      cursor: wait;
    }}

    @media (max-width: 640px) {{
      form {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <img src="/logo.png" alt="Retailware Softech" style="max-width:220px; height:auto; margin-bottom:16px;">
      <h1><span class="phone-icon">📞</span> Call Summary Tool</h1>
      <p class="sub">Analyze customer support calls in your browser.</p>
      <div class="hero-badges">
        <span class="badge">⚡ Instant Analysis</span>
        <span class="badge">🌐 Multi-language</span>
        <span class="badge">🤖 AI-Powered</span>
      </div>
      <a href="/keywords" class="btn btn-secondary">Manage Keywords</a>
    </header>

    <section class="panel">
      <form id="summaryForm" method="post" action="/summarize" enctype="multipart/form-data">
        <label class="dropzone" id="dropzone">
          <span class="dz-icon">🎧</span>
          <span class="dz-text">
            <span class="dz-title" id="dzTitle">Choose an audio file or drag it here</span>
            <span class="dz-sub">WAV, MP3, M4A, OGG, FLAC, MP4</span>
          </span>
          <input type="file" name="audio" id="audioInput" accept=".wav,.mp3,.m4a,.ogg,.flac,.mp4" required>
        </label>
        <button id="submitBtn" type="submit">Generate Summary</button>
      </form>
    </section>

    <div class="disclaimer">
      ⚠ This summary is generated using AI and may not be 100% accurate. <br>
      Please verify critical business information before use.
    </div>

    <section id="progressPanel" class="panel progress-panel">
      <p id="progressText" class="progress-text"><span class="spinner"></span>Processing...</p>
      <div class="progress-track">
        <div id="progressBar" class="progress-bar"></div>
      </div>
      <p id="progressPercent" class="progress-percent">0%</p>
    </section>

    {error_html}

    {token_html}

    {timing_html}

    {results_html}  

    <footer style="text-align:center; margin-top:40px; padding-top:24px; border-top:1px solid var(--border); color:var(--muted); font-size:12.5px;">
      <p style="margin:0;">© {time.strftime('%Y')} Retailware Softech Private Limited. All rights reserved.</p>
      <p style="margin:4px 0 0;">Internal tool — for authorized use only.</p>
    </footer>
</main>
  <script>
  (function() {{
    const dropzone = document.getElementById("dropzone");
    const audioInput = document.getElementById("audioInput");
    const dzTitle = document.getElementById("dzTitle");

    audioInput.addEventListener("change", () => {{
      if (audioInput.files.length > 0) {{
        dzTitle.textContent = audioInput.files[0].name;
      }}
    }});

    ["dragover", "dragenter"].forEach(evt => {{
      dropzone.addEventListener(evt, (e) => {{
        e.preventDefault();
        dropzone.classList.add("dragover");
      }});
    }});
    ["dragleave", "drop"].forEach(evt => {{
      dropzone.addEventListener(evt, () => dropzone.classList.remove("dragover"));
    }});
    dropzone.addEventListener("drop", (e) => {{
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      if (file) {{
        audioInput.files = e.dataTransfer.files;
        dzTitle.textContent = file.name;
      }}
    }});
  }})();

  (function() {{
    const form = document.getElementById("summaryForm");
    const panel = document.getElementById("progressPanel");
    const text = document.getElementById("progressText");
    const bar = document.getElementById("progressBar");
    const button = document.getElementById("submitBtn");

    const steps = [
      "🎙 Uploading audio...",
      "📝 Transcribing with Gemini...",
      "🔍 Analyzing transcript...",
      "✨ Generating insights...",
      "📄 Finalizing summary..."
    ];

    form.addEventListener("submit", (e) => {{
      e.preventDefault();
      document.body.classList.add("busy");
      panel.classList.add("active");
      button.disabled = true;
      dropzone.style.pointerEvents = "none";
      dropzone.style.opacity = "0.6";

      // Hide stale results from the previous file while new one processes
      const oldResults = document.getElementById("resultsPanel");
      const oldTokenBadge = document.getElementById("tokenBadge");
      const oldTimingBadge = document.getElementById("timingBadge");
      if (oldResults) oldResults.style.display = "none";
      if (oldTokenBadge) oldTokenBadge.style.display = "none";
      if (oldTimingBadge) oldTimingBadge.style.display = "none";

      const percentEl = document.getElementById("progressPercent");
      let index = 0;
      let progress = 0;
      text.innerHTML = '<span class="spinner"></span>' + steps[0];
      bar.style.width = "5%";
      percentEl.textContent = "5%";

      // Smooth continuous fill up to 90%, cycling through step labels
      const tick = setInterval(() => {{
        if (progress < 90) {{
          progress += 2;
          bar.style.width = progress + "%";
          percentEl.textContent = progress + "%";
        }}
        const newIndex = Math.min(steps.length - 1, Math.floor(progress / (90 / steps.length)));
        if (newIndex !== index) {{
          index = newIndex;
          text.innerHTML = '<span class="spinner"></span>' + steps[index];
        }}
      }}, 600);

      const formData = new FormData(form);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action, true);

      xhr.upload.onprogress = (evt) => {{
        if (evt.lengthComputable) {{
          const uploadPct = Math.round((evt.loaded / evt.total) * 100);
          if (uploadPct < 30) {{
            bar.style.width = uploadPct + "%";
            percentEl.textContent = uploadPct + "%";
            text.innerHTML = '<span class="spinner"></span>🎙 Uploading audio...';
          }}
        }}
      }};

      xhr.onload = () => {{
        clearInterval(tick);
        bar.style.width = "100%";
        bar.style.animation = "none";
        percentEl.textContent = "100%";
        text.innerHTML = "✅ Done!";
        setTimeout(() => {{
          document.open();
          document.write(xhr.responseText);
          document.close();
        }}, 300);
      }};

      xhr.onerror = () => {{
        clearInterval(tick);
        text.textContent = "❌ Something went wrong. Please try again.";
        bar.style.width = "0%";
        button.disabled = false;
        dropzone.style.pointerEvents = "auto";
        dropzone.style.opacity = "1";
      }};

      xhr.send(formData);
    }});
  }})();
  </script>
</body>
</html>"""

def render_keywords_page(error=""):
    keywords = "\n".join(testnew.load_keywords())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Manage Keywords - Call Summary Tool</title>
   <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --panel: #ffffff;
      --text: #1e293b;
      --muted: #64748b;
      --border: #d7dee7;
      --accent: #1c3f94;
      --accent-dark: #14306f;
      --danger: #dc2626;
      --warning: #e8a512;
    }}
    * {{ box-sizing: border-box; }}
   body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    main {{
      width: min(600px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      text-align: center;
      margin-bottom: 30px;
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      font-weight: 800;
      color: var(--accent);
      letter-spacing: -0.5px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 28px;
      box-shadow: 0 1px 2px rgba(15,23,42,0.04), 0 8px 24px rgba(15,23,42,0.06);
    }}
    textarea {{
      width: 100%;
      height: 400px;
      background: #f5f7fa;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 15px;
      font-family: 'JetBrains Mono', 'Consolas', monospace;
      font-size: 14px;
      resize: vertical;
    }}
    .actions {{
      margin-top: 20px;
      display: flex;
      gap: 10px;
      justify-content: center;
    }}
    .btn {{
      min-height: 45px;
      border: 0;
      border-radius: 8px;
      padding: 0 25px;
      background: var(--accent);
      color: #ffffff;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
      box-shadow: 0 2px 6px rgba(28,63,148,0.2);
    }}
    .btn:hover {{
      background: var(--accent-dark);
      transform: translateY(-3px);
      box-shadow: 0 10px 20px rgba(28,63,148,0.3);
    }}
    .btn-cancel {{
        background: #ffffff;
        border: 1px solid var(--border);
        color: var(--text);
        box-shadow: 0 1px 3px rgba(15,23,42,0.06);
    }}
    .btn-cancel:hover {{
        background: #f5f7fa;
        transform: translateY(-3px);
        box-shadow: 0 6px 14px rgba(15,23,42,0.1);
    }}
    .error {{
      color: var(--danger);
      margin-bottom: 15px;
      text-align: center;
    }}
    .modal-overlay {{
      position: fixed;
      inset: 0;
      background: rgba(15,23,42,0.5);
      backdrop-filter: blur(3px);
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s ease;
      z-index: 999;
    }}
    .modal-overlay.active {{
      opacity: 1;
      pointer-events: auto;
    }}
    .modal-box {{
      background: var(--panel);
      border-radius: 16px;
      padding: 32px;
      width: min(380px, 90vw);
      text-align: center;
      box-shadow: 0 20px 50px rgba(15,23,42,0.25);
      transform: scale(0.9) translateY(10px);
      transition: transform 0.25s cubic-bezier(.34,1.56,.64,1);
    }}
    .modal-overlay.active .modal-box {{
      transform: scale(1) translateY(0);
    }}
    .modal-icon {{
      font-size: 40px;
      margin-bottom: 12px;
    }}
    .modal-title {{
      margin: 0 0 8px;
      font-size: 19px;
      font-weight: 700;
      color: var(--text);
    }}
    .modal-text {{
      margin: 0 0 24px;
      font-size: 14px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .modal-actions {{
      display: flex;
      gap: 12px;
      justify-content: center;
    }}
    .modal-actions .btn {{
      flex: 1;
      min-height: 44px;
    }}
  </style>
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Manage Keywords</h1>
      <p style="color: var(--muted); font-size: 12px; margin-top: 8px;">Edit keywords (one per line)</p>
    </header>

    <section class="panel">
      {f'<div class="error">{html.escape(error)}</div>' if error else ''}
      <form id="keywordForm" method="post" action="/save_keywords">
        <textarea name="keywords">{html.escape(keywords)}</textarea>
        <div class="actions">
          <a href="/" class="btn btn-cancel">Back</a>
          <button type="submit" class="btn">Save & Close</button>
        </div>
      </form>
    </section>
</main>

  <div class="modal-overlay" id="confirmModal">
    <div class="modal-box">
      <div class="modal-icon">💾</div>
      <h3 class="modal-title">Save changes?</h3>
      <p class="modal-text">If Yes, Changes Will Get Save in Keyword List</p>
      <div class="modal-actions">
        <button type="button" class="btn btn-cancel" id="modalCancel">Cancel</button>
        <button type="button" class="btn" id="modalConfirm">Yes, Save</button>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById("keywordForm");
    const modal = document.getElementById("confirmModal");
    const confirmBtn = document.getElementById("modalConfirm");
    const cancelBtn = document.getElementById("modalCancel");

    form.addEventListener("submit", (e) => {{
      e.preventDefault();
      modal.classList.add("active");
    }});

    cancelBtn.addEventListener("click", () => {{
      modal.classList.remove("active");
    }});

    confirmBtn.addEventListener("click", () => {{
      modal.classList.remove("active");
      form.submit();
    }});

    modal.addEventListener("click", (e) => {{
      if (e.target === modal) modal.classList.remove("active");
    }});
  </script>
</body>
</html>"""


class CallSummaryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"DEBUG: Received GET request for {self.path}")
        try:
            if self.path in ("/", "/index.html"):
                self.respond_html(render_page())
            elif self.path == "/keywords":
                self.respond_html(render_keywords_page())
            elif self.path == "/logo.png":
                self.respond_file(BASE_DIR / "assets" / "logo_transparent.png", "image/png")
            elif self.path == "/favicon.ico":
              self.respond_file(BASE_DIR / "assets" / "retailware.ico", "image/x-icon")    
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            print(f"DEBUG: Error in do_GET: {e}")
            traceback.print_exc()
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
      if self.path == "/summarize":
        self.handle_summarize()
      elif self.path == "/save_keywords":
        self.handle_save_keywords()
      elif self.path == "/export_pdf":
        self.handle_export_pdf()
      else:
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_save_keywords(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            params = parse_qs(body)
            new_k = params.get('keywords', [''])[0].strip()
            
            with open("keywords.txt", "w", encoding="utf-8") as f:
                f.write(new_k)
            
            # Redirect back to home
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header('Location', '/')
            self.end_headers()
        except Exception as e:
            print(f"DEBUG: Error in handle_save_keywords: {e}")
            self.respond_html(render_keywords_page(error=str(e)), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_summarize(self):
        request_start = time.time()
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise RuntimeError("Upload must use multipart/form-data.")

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                },
            )
            upload = form["audio"] if "audio" in form else None
            if upload is None or not getattr(upload, "filename", ""):
                raise RuntimeError("Please choose an audio file.")

            filename = safe_filename(upload.filename)
            input_path = UPLOAD_DIR / filename
            os.makedirs(os.path.dirname(input_path), exist_ok=True)
            with open(input_path, "wb") as f:
                while True:
                    chunk = upload.file.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

            converted_file = None
            try:
                # Use logic from testnew
                converted_file = testnew.convert_audio(str(input_path))
                
                # analyze_audio_with_gemini returns:
                # (transcript, raw_output, facts, analysis, combined_summary, keywords)
                results = testnew.analyze_audio_with_gemini(converted_file)
                
                transcript = results[0]
                facts = results[2]
                analysis = results[3]
                summary = results[4]
                keywords = results[5]
                token_info = results[6]

                # Save files as in the desktop app
                testnew.save_transcript(transcript, str(input_path))
                testnew.save_summary(summary, analysis, facts, keywords, str(input_path))

                try:
                    db.save_call_summary(
                        filename=filename,
                        analysis_text=analysis,
                        keywords=keywords,
                        facts=facts,
                        summary=summary,
                        transcript=transcript,
                        token_info=token_info,
                        elapsed_time=time.time() - request_start
                    )
                except Exception as db_err:
                    print(f"[DB] Failed to save call summary: {db_err}")


                elapsed_time = time.time() - request_start
                display_transcript = strip_speaker_labels(transcript)
                self.respond_html(render_page(
                    analysis=analysis,
                    keywords=keywords,
                    facts=facts,
                    summary=summary,
                    transcript=display_transcript, 
                    filename=filename,
                    token_info=token_info,
                    elapsed_time=elapsed_time 
                ))
            finally:
                if converted_file and os.path.exists(converted_file) and converted_file != str(input_path):
                    try: os.remove(converted_file)
                    except: pass
        except Exception as e:
            details = str(e)
            print(traceback.format_exc())
            elapsed_time = time.time() - request_start
            self.respond_html(render_page(error=details), status=HTTPStatus.INTERNAL_SERVER_ERROR)
    def handle_export_pdf(self):       
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            params = parse_qs(body)
            
            analysis = params.get('analysis', [''])[0]
            keywords = params.get('keywords', [''])[0]
            facts = params.get('facts', [''])[0]
            summary = params.get('summary', [''])[0]
            transcript = params.get('transcript', [''])[0]
            filename = params.get('filename', ['call_summary'])[0]

            pdf_bytes = generate_pdf(analysis, keywords, facts, summary, transcript, filename)

            
            pdf_filename = Path(filename).stem + "_summary.pdf"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{pdf_filename}"')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.end_headers()
            self.wfile.write(pdf_bytes)
        except Exception as e:
            print(traceback.format_exc())
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))


    def respond_html(self, body, status=HTTPStatus.OK):
      try:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")   # ← key fix
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()
      except (ConnectionAbortedError, BrokenPipeError, OSError):
        pass   # Client disconnected — safe to ignore
    
    def respond_file(self, path, content_type):
        try:
            data = Path(path).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

def main():
    db.init_db()
    server = ThreadingHTTPServer((HOST, PORT), CallSummaryHandler)
    
    # Detect LAN IP
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        pass

    print(f"\n{'='*50}")
    print(f"Call Summary Web App is ACTIVE")
    print(f"Local access: http://127.0.0.1:{PORT}")
    print(f"LAN access:   http://{local_ip}:{PORT}")
    print(f"{'='*50}\n")
    server.serve_forever()

if __name__ == "__main__":
    main()