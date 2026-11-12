FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py dialogue.py telegram_notify.py prompt.txt ./
COPY messages ./messages
RUN mkdir /app/data && useradd -m appuser && chown -R appuser:appuser /app
USER appuser
ENV PORT=8000 PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --workers 1"]
