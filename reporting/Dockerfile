FROM python:3.11-slim
WORKDIR /app

COPY reporting/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reporting/report_lib.py .
COPY reporting/reporter.py .
# Shared with admin-ui -- the locked-dashboard generator report_lib builds on.
COPY admin-ui/dashboard_share.py .

CMD ["python", "-u", "reporter.py"]
