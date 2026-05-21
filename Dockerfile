FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    requests \
    google-cloud-storage \
    playwright==1.49.*

RUN playwright install chromium --with-deps

COPY entrypoint.py /app/
WORKDIR /app
CMD ["python", "entrypoint.py"]
