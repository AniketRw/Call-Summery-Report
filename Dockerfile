FROM python:3.10-slim

# ffmpeg + WeasyPrint system dependencies (Pango/Cairo handle proper
# Devanagari/Indic script shaping, unlike reportlab)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-noto \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code (includes fonts/ folder)
COPY . .

# Port
EXPOSE 8080

# Start
CMD ["python", "-u", "web_app.py"]
