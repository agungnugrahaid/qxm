"""Password hashing + lookup for customer portal logins (migration 028).

scrypt from the stdlib, so this adds no dependency -- the admin-ui image
already carries enough. Stored format:

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

The parameters are stored per-hash rather than assumed, so they can be raised
later without invalidating existing passwords.
"""

import base64
import hashlib
import hmac
import secrets

# ~100ms per verify on the VM: enough to make online guessing expensive
# without making the login page feel slow.
_N, _R, _P = 2 ** 15, 8, 1
_DKLEN = 32
# scrypt needs ~128*n*r bytes (32 MiB at these parameters) and OpenSSL's
# default maxmem is exactly 32 MiB, so it refuses with "memory limit
# exceeded" unless we raise it explicitly.
_MAXMEM = 128 * 1024 * 1024


def hash_password(password: str, n: int = _N, r: int = _R, p: int = _P) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. False on any malformed hash rather than raising --
    a corrupt row must fail the login, not 500 the page."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
            maxmem=_MAXMEM,
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def find_customer_user(conn, email: str):
    """-> row dict for an ACTIVE portal user, or None.

    Matched case-insensitively on email (the unique index is on lower(email)).
    Joins customers so the caller gets the display name without a second query.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.customer_id, u.email, u.password_hash, c.name AS customer_name
        FROM customer_users u
        JOIN customers c ON c.id = u.customer_id
        WHERE lower(u.email) = lower(%s) AND u.is_active
        """,
        (email,),
    )
    return cur.fetchone()


def touch_last_login(conn, user_id: int):
    cur = conn.cursor()
    cur.execute("UPDATE customer_users SET last_login_at = now() WHERE id = %s", (user_id,))
    conn.commit()
