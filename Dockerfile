FROM python:3.12-slim
WORKDIR /app
COPY r3_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY r3_bot /app/r3_bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "r3_bot"]
