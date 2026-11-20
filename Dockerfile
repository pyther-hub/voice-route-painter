FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY data data
COPY static static
ENV PYTHONUNBUFFERED=1
# one machine only: rate limits, spend caps and sessions live in this process's memory
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
