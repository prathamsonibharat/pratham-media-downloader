# Backend container (Render, Railway, Fly.io, a VPS…)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY engine.py app.py ./
COPY public ./public
ENV HOST=0.0.0.0 PORT=10000 BEHIND_PROXY=1 IG_KEEP_HOURS=1 RATE_LIMIT_PER_MIN=6 PYTHONUNBUFFERED=1
EXPOSE 10000
# 1 worker × 4 threads keeps memory low on free plans; downloads can take a while.
CMD gunicorn -b 0.0.0.0:${PORT} -w 1 --threads 4 --timeout 180 app:app
