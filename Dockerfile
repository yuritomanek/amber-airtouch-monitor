# Works on Raspberry Pi (arm64 / armv7) and x86.
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY test_connection.py monitor.py ./

# Default command runs the monitor. Starts in DRY_RUN unless you set
# DRY_RUN=false in your .env. To run the one-off connectivity test instead:
#   docker compose run --rm energy-automation python3 test_connection.py
CMD ["python3", "monitor.py"]
