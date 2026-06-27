FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip first to ensure clean package resolution
RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py env.txt ./
RUN mkdir -p downloads

CMD ["python", "-u", "bot.py"]
