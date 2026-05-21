FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

RUN pip install --no-cache-dir --break-system-packages \
    requests \
    google-cloud-storage \
    playwright

COPY entrypoint.py /app/
WORKDIR /app
CMD ["python", "entrypoint.py"]
