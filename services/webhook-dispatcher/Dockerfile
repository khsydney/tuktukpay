FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && opentelemetry-bootstrap -a install
COPY . .
# Same image, two roles (see docker-compose.yml):
#   webhook-dispatcher: opentelemetry-instrument python worker.py
#   merchant-sim:       opentelemetry-instrument uvicorn merchant_sim:app --host 0.0.0.0 --port 8085
CMD ["opentelemetry-instrument", "python", "worker.py"]
