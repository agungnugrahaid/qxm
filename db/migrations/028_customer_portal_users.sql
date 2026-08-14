-- 028: customer portal logins.
--
-- Lets a customer log in to QXM Console and see ONLY their own QoE data.
-- This replaces giving customers a Grafana account: in Grafana OSS any
-- authenticated Viewer can POST arbitrary SQL to /api/ds/query against any
-- datasource (datasource permissions are Enterprise-only), so a customer
-- login there is effectively read access to every customer's data and to
-- routers.admin_password. Here the customer_id comes from the signed session
-- and is applied server-side, so there is nothing for the client to tamper
-- with.
--
-- Password hashing is stdlib hashlib.scrypt (no new dependency); the stored
-- format is "scrypt$n$r$p$salt_b64$hash_b64". See admin-ui/portal_auth.py.

CREATE TABLE IF NOT EXISTS customer_users (
  id             SERIAL PRIMARY KEY,
  customer_id    INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  email          TEXT NOT NULL,
  password_hash  TEXT NOT NULL,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at  TIMESTAMPTZ
);

-- Login is by email, so it must be unique across ALL customers -- otherwise
-- the lookup is ambiguous and could resolve to the wrong customer_id.
-- Case-insensitive: operators will enter addresses inconsistently.
CREATE UNIQUE INDEX IF NOT EXISTS customer_users_email_key
  ON customer_users (lower(email));

CREATE INDEX IF NOT EXISTS idx_customer_users_customer
  ON customer_users (customer_id);
