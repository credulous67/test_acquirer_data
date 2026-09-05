FROM python:3.12-slim
WORKDIR /app

COPY services/dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/common /app/services/common
COPY services/dashboard /app/services/dashboard

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "services.dashboard.app"]
