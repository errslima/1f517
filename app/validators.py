"""Mechanical validation: everything rejectable without semantic judgment.
Whatever passes here still requires Warden screening before going live.
Error codes match docs/specs/finding-schema.md."""
import json, re
from . import config

SUBJECT_RE = {
    "pkg":   re.compile(r"^pkg:[a-z0-9._+-]+/[a-z0-9@._/+-]+$"),
    "api":   re.compile(r"^api:[a-z0-9.-]+\.[a-z]{2,}(/[a-z0-9._~/{}%+-]*)?$"),
    "model": re.compile(r"^model:[a-z0-9._-]+/[a-z0-9._\[\]-]+$"),
    "tool":  re.compile(r"^tool:[a-z0-9._+-]+$"),
    "paper": re.compile(r"^paper:(doi|arxiv)/[a-z0-9./()-]+$"),
    "spec":  re.compile(r"^spec:[a-z0-9._-]+/[a-z0-9._-]+$"),
}
PRIVATE_HOST = re.compile(
    r"(^|[^a-z0-9])(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[a-z0-9-]+\.(local|internal|corp|lan))([^a-z0-9]|$)")

SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|ghp|gho|ghu|hv|qc|xoxb|xoxp)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I),           # email
    re.compile(r"\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", re.I),
]

# Imperative lint: conservative, mechanical. The Warden does the semantic pass.
IMPERATIVE_START = re.compile(
    r"(^|[.!?]\s+)(run|execute|install|visit|click|download|open|type|paste|"
    r"ignore|disable|enable|delete|remove|set|add|use|call|fetch|curl|wget|"
    r"pip|npm|apt|follow|go to|make sure|be sure|you should|you must|please)\b", re.I)
COMMAND_BLOCK = re.compile(r"(```|\$\s+\w+|^\s*(curl|wget|bash|sh|python|pip|npm|npx|sudo|rm|chmod)\s)", re.M)
URL_RE = re.compile(r"https?://", re.I)
HEDGES = re.compile(r"\b(might|may possibly|sometimes worth|could try|worth trying|perhaps)\b", re.I)


def normalize_subject(s: str):
    """Return (canonical_subject, error). Lowercases, strips, validates shape."""
    if not isinstance(s, str):
        return None, "subject_not_public"
    s = s.strip().lower().rstrip("/")
    kind = s.split(":", 1)[0] if ":" in s else None
    rx = SUBJECT_RE.get(kind)
    if not rx or not rx.match(s) or " " in s:
        return None, "subject_not_public"
    if kind == "api" and PRIVATE_HOST.search(s.split(":", 1)[1]):
        return None, "subject_not_public"
    return s, None


def lint_prose(text: str, field: str, max_len: int, urls_allowed: bool = False):
    """Returns list of error codes for one prose field."""
    errs = []
    if text is None:
        return errs
    if len(text) > max_len:
        errs.append(f"field_too_long:{field}")
    if IMPERATIVE_START.search(text) or COMMAND_BLOCK.search(text):
        errs.append(f"imperative_content:{field}")
    if not urls_allowed and URL_RE.search(text):
        errs.append(f"imperative_content:{field}")   # URLs only in refs
    for p in SECRET_PATTERNS:
        if p.search(text):
            errs.append(f"possible_secret:{field}")
            break
    return errs


def validate_applicability(app_list):
    if not isinstance(app_list, list) or len(app_list) == 0:
        return ["missing_applicability"]
    errs = []
    for i, entry in enumerate(app_list):
        if not isinstance(entry, dict):
            errs.append("missing_applicability"); continue
        if entry.get("field") not in config.APPLICABILITY_FIELDS:
            errs.append(f"missing_applicability:bad_field:{entry.get('field')}")
        if entry.get("op") not in config.APPLICABILITY_OPS:
            errs.append(f"missing_applicability:bad_op:{entry.get('op')}")
        v = entry.get("value")
        if v is None or (isinstance(v, str) and not v.strip()):
            errs.append("missing_applicability:empty_value")
        if isinstance(v, str):
            errs += lint_prose(v, f"applicability[{i}].value", 200)
    return errs


def validate_observation(body: dict):
    errs = []
    subject, e = normalize_subject(body.get("subject", ""))
    if e: errs.append(e)
    event = body.get("event", "")
    if not isinstance(event, str) or not re.match(r"^[a-z0-9_]{2,40}$", event):
        errs.append("field_too_long:event")
    errs += lint_prose(body.get("detail"), "detail", 280)
    ctx = body.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict) or len(ctx) > 10:
            errs.append("field_too_long:context")
        else:
            for k, v in ctx.items():
                errs += lint_prose(str(v), f"context.{k}", 100)
    return subject, errs


def validate_finding(body: dict):
    errs = []
    subject, e = normalize_subject(body.get("subject", ""))
    if e: errs.append(e)

    claim = body.get("claim", "")
    if not claim or not isinstance(claim, str):
        errs.append("field_too_long:claim")
    else:
        errs += lint_prose(claim, "claim", 500)
        if HEDGES.search(claim):
            errs.append("unfalsifiable")

    errs += validate_applicability(body.get("applicability"))

    verify = body.get("verify")
    if not isinstance(verify, dict) or verify.get("method") not in config.VERIFY_METHODS:
        errs.append("unfalsifiable")
    else:
        errs += lint_prose(verify.get("expectation", ""), "verify.expectation", 1000)
        if not verify.get("expectation"):
            errs.append("unfalsifiable")

    fb = body.get("falsified_by")
    if not fb or not isinstance(fb, str):
        errs.append("unfalsifiable")
    else:
        errs += lint_prose(fb, "falsified_by", 500)

    ttl = body.get("ttl_days")
    cap = config.TTL_CAPS.get(subject.split(":", 1)[0]) if subject else None
    if ttl is None and cap:
        ttl = cap
    if not isinstance(ttl, int) or ttl < 1:
        errs.append("ttl_exceeds_cap")
    elif cap and ttl > cap:
        errs.append("ttl_exceeds_cap")

    refs = body.get("refs", [])
    if not isinstance(refs, list) or len(refs) > 10 or any(not isinstance(r, str) or len(r) > 300 for r in refs):
        errs.append("field_too_long:refs")

    return subject, ttl, errs
