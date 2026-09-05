FROM python:3.12-slim
WORKDIR /app

COPY services/seed_generator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/common /app/services/common
COPY scripts/generate_data.py /app/scripts/generate_data.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "scripts/generate_data.py", "--out", "/data", "--if-missing"]
CMD ["--merchants", "250", "--cards", "20000", "--authorizations", "200000", "--days-back", "30", "--seed", "42"]
