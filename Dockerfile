FROM python:3.10-slim

# ffmpeg install
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

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