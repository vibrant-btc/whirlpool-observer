FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WHIRLPOOL_DATA_DIR=/data \
    WHIRLPOOL_REPORTS_DIR=/reports \
    WHIRLPOOL_WEB_HOST=0.0.0.0 \
    WHIRLPOOL_WEB_PORT=8080 \
    WHIRLPOOL_RESCAN_HOURS=12 \
    WHIRLPOOL_ONION_LOCATION=

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ashidetector.py ./
COPY observer.html ./
COPY assets ./assets

RUN mkdir -p /data /reports

EXPOSE 8080

ENTRYPOINT ["python", "ashidetector.py"]
CMD ["run"]
