FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && opentelemetry-bootstrap -a install
# Uncomment to enable real providers: RUN pip install openai anthropic boto3
COPY . .
EXPOSE 8086
CMD ["opentelemetry-instrument", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8086", "--no-access-log"]
