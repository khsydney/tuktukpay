FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && opentelemetry-bootstrap -a install
# Uncomment to enable real providers: RUN pip install openai anthropic boto3
COPY . .
# Same image, two roles (see docker-compose.yml):
#   shopping-agent (console UI): opentelemetry-instrument uvicorn shopping_agent:app --host 0.0.0.0 --port 8087
#   wallet-sim (mandates + KYA): opentelemetry-instrument uvicorn wallet_sim:app --host 0.0.0.0 --port 8088
EXPOSE 8087 8088
CMD ["opentelemetry-instrument", "uvicorn", "shopping_agent:app", "--host", "0.0.0.0", "--port", "8087", "--no-access-log"]
