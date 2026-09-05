FROM python:3.12-slim
WORKDIR /app

COPY services/issuer_simulator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/common /app/services/common
COPY services/issuer_simulator /app/services/issuer_simulator

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "services.issuer_simulator.app"]
