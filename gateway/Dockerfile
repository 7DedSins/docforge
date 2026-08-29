FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docforgectl.py ./docforgectl.py

EXPOSE 8000

# Single worker on purpose. The concurrency limits that protect the media stack
# are per-process semaphores; multiple workers would multiply them.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
