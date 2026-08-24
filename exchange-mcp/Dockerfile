FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
# Railway injects PORT automatically; server.py switches to Streamable HTTP
# when PORT is set, and to stdio otherwise (for local dev).
CMD ["python", "server.py"]
