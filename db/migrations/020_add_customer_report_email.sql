-- Where the monthly reporter emails this customer's QoE PDF (see
-- reporting/). NULL = don't email; the on-demand download button in
-- admin-ui works regardless.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS report_email TEXT;
