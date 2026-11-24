FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd -m -u 10001 appuser && mkdir -p /app/data /shards/node-0 /shards/node-1 && chown -R appuser:appuser /app/data /shards
COPY backend ./backend
COPY cleaning ./cleaning
COPY db ./db
COPY frontend ./frontend
COPY sample ./sample
ENV PYTHONUNBUFFERED=1 CHECKIN_DATA=/app/data
USER appuser
CMD ["python", "-m", "backend.app", "--host", "0.0.0.0", "--no-worker"]
