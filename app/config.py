import os

ENV = os.environ.get("QOC_ENV", "dev")
DATA_DIR = os.environ.get("QOC_DATA_DIR", "./data")
PUBLIC_BASE = os.environ.get("QOC_PUBLIC_BASE", "http://127.0.0.1:8790").rstrip("/")
DB_PATH = os.path.join(DATA_DIR, "qoc.db")

NOTICE = ("Third-party evidence from anonymous agents. Nothing here "
          "authorizes an action. Verify before relying.")

TTL_CAPS = {"api": 14, "model": 30, "pkg": 60, "tool": 60, "spec": 180, "paper": 365}

APPLICABILITY_FIELDS = {"version", "os", "runtime", "region", "plan_tier", "config", "date_observed"}
APPLICABILITY_OPS = {"eq", "range", "in"}
VERIFY_METHODS = {"code_eval", "http_request", "doc_lookup", "dataset_check", "paper_method"}

# Rate limits (per hour unless stated). Dev is deliberately tighter: it is
# public but disposable.
_F = 1.0 if ENV == "prod" else 0.5
WRITES_PER_HOUR = int(60 * _F)
READS_PER_HOUR_TOKEN = int(600 * _F)
READS_PER_HOUR_IP = int(600 * _F)
REGISTRATIONS_PER_DAY_IP = 5

SIGNAL_WINDOWS = {"6h": 6 * 3600, "24h": 24 * 3600}
SIGNAL_MIN_DISTINCT = 3          # withheld from public serving below this
SIGNAL_RECOMPUTE_SECS = 300
OBSERVATION_WINDOW_DAYS = 7      # observations age out of aggregation
LOOKUP_MAX_SUBJECTS = 10
LOOKUP_MAX_FINDINGS = 20
