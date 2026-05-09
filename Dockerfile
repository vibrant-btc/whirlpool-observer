FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WHIRLPOOL_DATA_DIR=/data \
    WHIRLPOOL_REPORTS_DIR=/reports

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ashidetector.py ./

RUN mkdir -p /data /reports

ENTRYPOINT ["python", "ashidetector.py"]
CMD ["run"]
