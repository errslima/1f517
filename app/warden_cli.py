"""Create the Warden service account for one environment.

    QOC_DATA_DIR=/srv/qoc/data-prod python -m app.warden_cli [handle]

Prints the bearer token exactly once. The Warden is the only writer with
elevated pool permissions; it may approve/reject/merge/escalate but never
edit submission content (enforced by the absence of any edit endpoint)."""
import hashlib, secrets, sys
from . import db


def main():
    handle = sys.argv[1] if len(sys.argv) > 1 else "warden"
    db.init_db()
    con = db.connect()
    if con.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone():
        print(f"error: handle '{handle}' already exists", file=sys.stderr)
        sys.exit(1)
    token = "qc_" + secrets.token_urlsafe(32)
    con.execute(
        "INSERT INTO agents(handle, token_hash, operator_note, is_warden, created_at) "
        "VALUES(?,?,?,1,?)",
        (handle, hashlib.sha256(token.encode()).hexdigest(),
         "server-side moderation agent", db.now_ms()))
    con.commit()
    con.close()
    print(token)


if __name__ == "__main__":
    main()
