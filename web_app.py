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
from urllib.parse import parse_qs
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Preformatted
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import Preformatted
import io

import subprocess
import tempfile
import shutil

from reportlab.platypus import Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

def register_fonts():
    font_path = BASE_DIR / "fonts" / "NotoSansDevanagari.ttf"

    if font_path.exists():
        pdfmetrics.registerFont(
            TTFont("Devanagari", str(font_path))
        )
        print("Loaded font:", font_path)
        return "Devanagari"

    print("Font not found:", font_path)
    return "Helvetica"


def generate_pdf(
    analysis,
    keywords,
    facts,
    summary,
    transcript,
    filename="call_summary"
):
    font_name = register_fonts()
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20,
        leftMargin=20,
        topMargin=20,
        bottomMargin=20,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
      "Title",
      parent=styles["Heading1"],
      fontName=font_name,
      alignment=TA_CENTER,
      textColor=colors.darkblue,
      fontSize=20,
      spaceAfter=15,
    )

    heading_style = ParagraphStyle(
      "Heading",
      parent=styles["Heading2"],
      fontName=font_name,
      textColor=colors.white,
      backColor=colors.darkgreen,
      spaceBefore=10,
      spaceAfter=6,
      leftIndent=5,
    )

    body_style = ParagraphStyle(
      "Body",
      parent=styles["BodyText"],
      fontName=font_name,
      fontSize=10,
      leading=16,
      spaceAfter=12,
    )

    story = []

    story.append(Paragraph("📞 Call Summary Report", title_style))
    story.append(
        Paragraph(f"<b>File:</b> {html.escape(filename)}", body_style)
    )
    story.append(Spacer(1, 12))

    sections = [
        ("ANALYSIS", analysis),
        ("KEYWORDS IDENTIFIED", keywords),
        ("CALL DETAILS", facts),
        ("SUMMARY", summary),
        ("TRANSCRIPT", transcript),
    ]

    for heading, content in sections:
      story.append(Paragraph(heading, heading_style))

      if heading == "TRANSCRIPT":
        story.append(Preformatted(content or "", body_style))
      else:
        text = html.escape(content or "")
        text = text.replace("\n", "<br/>")
        story.append(Paragraph(text, body_style))

      story.append(Spacer(1, 10))

    doc.build(story)

    pdf = buffer.getvalue()
    buffer.close()

    return pdf
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
      <div class="token-badge">
        🪙 Gemini Tokens Used : <b>{token_info['total']}</b>
      </div>
      """
      timing_html = ""
      if elapsed_time is not None:
        timing_html = f"""
        <div class="token-badge timing-badge">
          ⏱ Processing Time : <b>{html.escape(format_duration(elapsed_time))}</b>
        </div>
        """
      
    if any([analysis, keywords, facts, summary, transcript]):
        results_html = f"""
        <section class="panel">
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
            <button type="submit" style="background:#ffcc00; color:#0f1117;">⬇ Export PDF</button>
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
  <style>
    .token-badge{{
      display:inline-block;
      margin-bottom:18px;
      padding:8px 14px;
      border:1px solid #2d313d;
      border-radius:6px;
      background:#1a1d27;
      color:#00ff99;
      font-weight:bold;
      font-size:15px;
      .timing-badge{{color:#ffcc00; }}
    }}
    :root {{
      color-scheme: dark;
      --bg: #0f1117;`
      --panel: #1a1d27;
      --text: #e0e0e0;
      --muted: #888888;
      --border: #2d313d;
      --accent: #00ff99;
      --accent-dark: #00cc7a;
      --danger: #ff5555;
      --warning: #ffcc00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Courier New', Courier, monospace;
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
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      color: var(--accent);
      letter-spacing: 1px;
    }}
    .sub {{
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 20px;
    }}
    form {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 15px;
      align-items: center;
    }}
    input[type="file"] {{
      width: 100%;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #0f1117;
      color: var(--text);
    }}
    button, .btn {{
      min-height: 45px;
      border: 0;
      border-radius: 6px;
      padding: 0 25px;
      background: var(--accent);
      color: #0f1117;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    button:hover, .btn:hover {{ background: var(--accent-dark); }}
    .btn-secondary {{
        background: var(--warning);
        margin-top: 10px;
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
      line-height: 1.5;
      font-size: 14px;
      background: #0f1117;
      padding: 15px;
      border-radius: 4px;
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
    .progress-track {{
      width: 100%;
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: #0f1117;
      margin: 15px 0;
    }}
    .progress-bar {{
      width: 0%;
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
      transition: width 500ms ease;
    }}
    .progress-text {{
      margin: 0;
      color: var(--warning);
      font-size: 16px;
    }}
    .busy button {{
      opacity: 0.5;
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
      <h1>📞 Call Summary Tool</h1>
      <p class="sub">Analyze customer support calls in your browser.</p>
      <a href="/keywords" class="btn btn-secondary">Manage Keywords</a>
    </header>

    <section class="panel">
      <form id="summaryForm" method="post" action="/summarize" enctype="multipart/form-data">
        <input type="file" name="audio" accept=".wav,.mp3,.m4a,.ogg,.flac,.mp4" required>
        <button id="submitBtn" type="submit">Generate Summary</button>
      </form>
    </section>

    <div class="disclaimer">
      ⚠ This summary is generated using AI and may not be 100% accurate. <br>
      Please verify critical business information before use.
    </div>

    <section id="progressPanel" class="panel progress-panel">
      <p id="progressText" class="progress-text">Processing...</p>
      <div class="progress-track">
        <div id="progressBar" class="progress-bar"></div>
      </div>
    </section>

    {error_html}

    {token_html}

    {timing_html}

    {results_html}  
</main>
  <script>
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

      let index = 0;
      let progress = 0;
      text.textContent = steps[0];
      bar.style.width = "5%";

      // Smooth continuous fill up to 90%, cycling through step labels
      const tick = setInterval(() => {{
        if (progress < 90) {{
          progress += 2;
          bar.style.width = progress + "%";
        }}
        const newIndex = Math.min(steps.length - 1, Math.floor(progress / (90 / steps.length)));
        if (newIndex !== index) {{
          index = newIndex;
          text.textContent = steps[index];
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
            text.textContent = "🎙 Uploading audio... " + uploadPct + "%";
          }}
        }}
      }};

      xhr.onload = () => {{
        clearInterval(tick);
        bar.style.width = "100%";
        text.textContent = "✅ Done!";
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
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1117;
      --panel: #1a1d27;
      --text: #e0e0e0;
      --muted: #888888;
      --border: #2d313d;
      --accent: #00ff99;
      --accent-dark: #00cc7a;
      --danger: #ff5555;
      --warning: #ffcc00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: 'Courier New', Courier, monospace;
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
      font-size: 24px;
      color: var(--accent);
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
    }}
    textarea {{
      width: 100%;
      height: 400px;
      background: #0f1117;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 15px;
      font-family: inherit;
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
      border-radius: 6px;
      padding: 0 25px;
      background: var(--accent);
      color: #0f1117;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .btn:hover {{ background: var(--accent-dark); }}
    .btn-cancel {{
        background: var(--panel);
        border: 1px solid var(--border);
        color: var(--text);
    }}
    .btn-cancel:hover {{ background: #2d313d; }}
    .error {{
      color: var(--danger);
      margin-bottom: 15px;
      text-align: center;
    }}
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
  <script>
    const form = document.getElementById("keywordForm");
    form.addEventListener("submit", (e) => {{
      if (!confirm("Do you want to save these changes?")) {{
        e.preventDefault();
      }}
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
                elapsed_time = time.time() - request_start
                
                self.respond_html(render_page(
                    analysis=analysis,
                    keywords=keywords,
                    facts=facts,
                    summary=summary,
                    transcript=transcript,
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

            if pdf_bytes is None:
              self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "PDF export is unavailable because Chrome/Edge is not installed."
              )
              return
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

def main():
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