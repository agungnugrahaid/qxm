FROM python:3.11-slim
WORKDIR /app

# WeasyPrint (HTML->PDF) needs pango for text layout; Inter is the report
# typeface (Lumina Console design system).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 fonts-inter \
    && rm -rf /var/lib/apt/lists/*

COPY reporting/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reporting/report_lib.py .
COPY reporting/reporter.py .
COPY reporting/report_template.html .
COPY reporting/assets/ ./assets/
# Shared with admin-ui -- the locked-dashboard generator report_lib builds on.
COPY admin-ui/dashboard_share.py .

CMD ["python", "-u", "reporter.py"]
