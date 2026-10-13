FROM python:3.14-slim

# Send print/logging output straight to Cloud Logging (no buffering)
ENV PYTHONUNBUFFERED=1

# Run as an unprivileged user instead of root
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Install dependencies first (as root) so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the code, owned by the app user so it is readable whatever the file modes were
COPY --chown=app:app . .

USER app

# Run the script when the container starts, then exit
CMD ["python", "main.py"]
