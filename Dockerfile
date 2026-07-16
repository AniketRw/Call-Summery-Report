FROM python:3.10-slim

# ffmpeg + chromium (for PDF export) + fonts + required libs install
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    chromium \
    fonts-noto \
    fonts-noto-color-emoji \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code
COPY . .

# Port
EXPOSE 8080

# Start
CMD ["python", "-u", "web_app.py"]
