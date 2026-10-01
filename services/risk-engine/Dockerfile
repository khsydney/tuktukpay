FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && opentelemetry-bootstrap -a install
COPY . .
EXPOSE 8081
# Zero-code instrumentation: the Splunk distro is discovered by opentelemetry-instrument.
CMD ["opentelemetry-instrument", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
