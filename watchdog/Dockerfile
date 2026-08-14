# python-slim rather than alpine: the incident detector needs psycopg2, which
# ships no musl wheels, so on alpine it would have to be compiled from source.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir psycopg2-binary==2.9.9
COPY watchdog.sh /watchdog.sh
COPY detector.py /detector.py
RUN chmod +x /watchdog.sh
# Both are infinite loops. The detector runs in the foreground so the container
# dies (and restart:unless-stopped revives it) if it ever exits; the health-check
# script runs behind it. NOTE: `wait -n` is not available -- /bin/sh here is
# dash, not bash.
CMD ["/bin/sh", "-c", "/watchdog.sh & exec python -u /detector.py"]
