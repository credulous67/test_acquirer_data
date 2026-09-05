FROM python:3.12-slim
WORKDIR /app

COPY services/gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/common /app/services/common
COPY services/gateway /app/services/gateway

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "services.gateway.app"]
