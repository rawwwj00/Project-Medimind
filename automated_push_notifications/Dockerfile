FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]