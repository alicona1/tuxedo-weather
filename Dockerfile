FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Saved zip lookup and last weather data, so a restart during an internet
# outage still has something to serve. Mount a volume here.
RUN useradd --system --uid 10086 --no-create-home app \
    && mkdir -p /data && chown app /data
ENV CACHE_DIR=/data
VOLUME /data

USER app

# Port the panel firmware connects to
EXPOSE 10086

CMD ["python", "app.py"]
