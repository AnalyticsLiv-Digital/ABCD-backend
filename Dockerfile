FROM python:3.11-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN git clone --depth 1 https://github.com/google-marketing-solutions/abcds-detector.git abcd_original

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]