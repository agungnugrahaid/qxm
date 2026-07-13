FROM python:3.11-slim
WORKDIR /app

COPY admin-ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY admin-ui/ .
# Shared deploy logic + script templates live in routeros/, pulled in at
# build time so this image doesn't depend on a runtime mount.
COPY routeros/deploy_lib.py ./deploy_lib.py
COPY routeros/qoe-push-metrics-v7.rsc ./qoe-push-metrics-v7.rsc
COPY routeros/qoe-push-metrics-v6.rsc ./qoe-push-metrics-v6.rsc
COPY routeros/qoe-push-firmware-v7.rsc ./qoe-push-firmware-v7.rsc
COPY routeros/qoe-push-firmware-v6.rsc ./qoe-push-firmware-v6.rsc
COPY routeros/qoe-baseline-hardening-v7.rsc ./qoe-baseline-hardening-v7.rsc
COPY routeros/qoe-baseline-hardening-v6.rsc ./qoe-baseline-hardening-v6.rsc
# Shared PDF-report logic (also used by the reporter container).
COPY reporting/report_lib.py ./report_lib.py

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8082"]
