"""
reporter.py -- monthly customer QoE PDF emails.

Loop modeled on config-snapshot-watcher: wake hourly; when it's the 1st
of the month during the 08:00 WIB hour and this month's run hasn't
happened yet (state file), generate every customer's PDF via
report_lib.generate_report and email it to customers.report_email.
Customers with no report_email are skipped with a log line -- the
on-demand download button in admin-ui covers them.

`python reporter.py --once` runs a single cycle immediately regardless of
date/state (used for testing via docker compose exec) -- it does NOT
update the state file, so a test run never suppresses the real monthly one.
"""

import os
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import psycopg2
import psycopg2.extras

from report_lib import generate_report, slugify

DATABASE_URL = os.environ["DATABASE_URL"]
SMTP_HOST = os.environ.get("SMTP_HOST", "changeme")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "changeme")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "changeme")
REPORT_FROM_ADDRESS = os.environ.get("REPORT_FROM_ADDRESS", "reports@yourisp.com")

STATE_FILE = "/state/last-run-month"
WIB = timezone(timedelta(hours=7))
REPORT_DAYS = 30


def send_email(to_address, customer_name, pdf_bytes, period_label):
    msg = EmailMessage()
    msg["From"] = REPORT_FROM_ADDRESS
    msg["To"] = to_address
    msg["Subject"] = f"Network Quality Report / Laporan Kualitas Jaringan - {customer_name} - {period_label}"
    msg.set_content(
        "Please find attached your monthly network quality report.\n"
        "Terlampir laporan kualitas jaringan bulanan Anda.\n"
    )
    filename = f"qoe-report-{slugify(customer_name)}-{period_label}.pdf"
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def run_cycle():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()
    cur.execute("SELECT id, name, report_email FROM customers ORDER BY name")
    customers = cur.fetchall()
    conn.close()

    period_label = datetime.now(WIB).strftime("%Y-%m")
    ok, skipped, failed = 0, 0, 0
    for c in customers:
        if not c["report_email"]:
            print(f"[{c['name']}] skipped -- no report_email set", flush=True)
            skipped += 1
            continue
        try:
            _, pdf_bytes = generate_report(c["id"], days=REPORT_DAYS)
            send_email(c["report_email"], c["name"], pdf_bytes, period_label)
            print(f"[{c['name']}] report emailed to {c['report_email']} ({len(pdf_bytes)} bytes)", flush=True)
            ok += 1
        except Exception as e:
            # One customer's failure (bad address, SMTP hiccup, render
            # timeout) must not abort everyone else's report.
            print(f"[{c['name']}] FAILED -- {e}", flush=True)
            failed += 1
    print(f"cycle done: {ok} emailed, {skipped} skipped, {failed} failed", flush=True)


def month_key(dt):
    return dt.strftime("%Y-%m")


def already_ran_this_month(now):
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() == month_key(now)
    except FileNotFoundError:
        return False


def mark_ran(now):
    with open(STATE_FILE, "w") as f:
        f.write(month_key(now))


def main_loop():
    print("reporter started -- monthly run on the 1st, 08:00-08:59 WIB", flush=True)
    while True:
        now = datetime.now(WIB)
        if now.day == 1 and now.hour == 8 and not already_ran_this_month(now):
            print(f"monthly run starting ({month_key(now)})", flush=True)
            # Mark BEFORE running: a mid-run crash should not cause every
            # customer to be re-emailed on the next hourly wake-up --
            # missing reports can be sent manually via the admin-ui
            # button, duplicate emails to customers can't be unsent.
            mark_ran(now)
            run_cycle()
        time.sleep(3600)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_cycle()
    else:
        main_loop()
