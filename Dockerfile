FROM python:3.12-slim
WORKDIR /app
COPY trend_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY trend_bot /app/trend_bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "trend_bot"]
