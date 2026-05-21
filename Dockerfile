FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

RUN pip install --no-cache-dir \
    requests \
    google-cloud-storage

COPY entrypoint.py /app/
WORKDIR /app
CMD ["python", "entrypoint.py"]
