FROM python:3.11-slim
WORKDIR /app

# WeasyPrint (HTML->PDF) needs pango for text layout. Montserrat is the
# report typeface (GMedia company design language); no Debian package
# exists so the TTFs are vendored in reporting/assets/fonts/.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY reporting/assets/fonts/ /usr/share/fonts/truetype/montserrat/

COPY reporting/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reporting/report_lib.py .
COPY reporting/charts.py .
COPY reporting/reporter.py .
COPY reporting/report_template.html .
COPY reporting/assets/ ./assets/
# Shared with admin-ui -- the locked-dashboard generator report_lib builds on.
COPY admin-ui/dashboard_share.py .

CMD ["python", "-u", "reporter.py"]
