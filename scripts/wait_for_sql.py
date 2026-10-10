"""Wait until SQL Server accepts connections and answers a query.

Used by CI right after the container starts (SQL Server takes a while to
come up, and the official image has no health check we can rely on).

    export SQLVEC_CONN="Server=localhost,1433;Database=master;UID=sa;PWD=..."
    python scripts/wait_for_sql.py --timeout 180
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between tries")
    args = parser.parse_args()

    conn_str = os.environ.get("SQLVEC_CONN")
    if not conn_str:
        print("set SQLVEC_CONN", file=sys.stderr)
        return 2

    import mssql_python

    deadline = time.monotonic() + args.timeout
    attempt = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempt += 1
        try:
            conn = mssql_python.connect(conn_str, autocommit=True, timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)), @@VERSION"
                )
                version, banner = cur.fetchone()
            finally:
                conn.close()
            print(f"SQL Server is up after {attempt} attempt(s): {version}")
            print(" ".join(str(banner).split()))
            return 0
        except Exception as exc:
            last_error = " ".join(str(exc).split())[:200]
            print(f"attempt {attempt}: not ready ({last_error})", flush=True)
            time.sleep(args.interval)
    print(f"SQL Server did not come up within {args.timeout:.0f}s: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
