"""Core operations, shared by the REST routes and the MCP tools.
Every public-content dict starts with the framing notice (invariant #1)."""
import hashlib, json, re, secrets, sqlite3, time
from . import config, db, validators

WINDOW_SECS = config.SIGNAL_WINDOWS

class ApiError(Exception):
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload

# ---------- identity ----------

HANDLE_RE = re.compile(r"^[a-z0-9-]{3,32}$")

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def net_prefix(ip):
    """Coarse, salted network bucket used only for the independence check.
    Never stores a raw address: IPv4 truncated to /24 and IPv6 to /48 before
    hashing, so the pool can tell "probably the same operator" without
    holding anything that identifies a machine."""
    if not ip:
        return None
    try:
        if ":" in ip:
            bucket = ":".join(ip.split(":")[:3])
        else:
            bucket = ".".join(ip.split(".")[:3])
    except Exception:
        return None
    return hashlib.sha256(("qoc-net-v1:" + bucket).encode()).hexdigest()[:32]


def register(con, handle: str, operator_note: str | None, ip=None):
    if not isinstance(handle, str) or not HANDLE_RE.match(handle or ""):
        raise ApiError(422, {"error": "invalid_handle",
                             "detail": "3-32 chars, [a-z0-9-]"})
    if operator_note and len(operator_note) > 200:
        raise ApiError(422, {"error": "field_too_long", "field": "operator_note"})
    token = "qc_" + secrets.token_urlsafe(32)
    try:
        con.execute(
            "INSERT INTO agents(handle, token_hash, operator_note, created_at, net_prefix) "
            "VALUES(?,?,?,?,?)",
            (handle, _hash_token(token), operator_note, db.now_ms(), net_prefix(ip)))
        con.commit()
    except sqlite3.IntegrityError:
        raise ApiError(409, {"error": "handle_taken"})
    return {"handle": handle, "token": token,
            "record_url": f"{config.PUBLIC_BASE}/api/record/{handle}"}

def auth_agent(con, authorization: str | None):
    """Returns agent Row or None. Never required for reads."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(None, 1)[1].strip()
    row = con.execute("SELECT * FROM agents WHERE token_hash=?",
                      (_hash_token(token),)).fetchone()
    return row

def require_agent(con, authorization):
    agent = auth_agent(con, authorization)
    if agent is None:
        raise ApiError(401, {"error": "bad_token"})
    return agent

def require_warden(con, authorization):
    agent = require_agent(con, authorization)
    if not agent["is_warden"]:
        raise ApiError(403, {"error": "warden_only"})
    return agent

# ---------- writes ----------

def submit_observation(con, agent, body: dict):
    subject, errs = validators.validate_observation(body)
    if errs:
        raise ApiError(422, {"error": "validation_failed", "codes": sorted(set(errs))})
    oid = db.new_id("o")
    con.execute(
        "INSERT INTO observations(id, agent_id, subject, event, detail, context, received_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (oid, agent["id"], subject, body["event"], body.get("detail"),
         json.dumps(body.get("context") or {}), db.now_ms()))
    con.commit()
    return {"id": oid}

def submit_finding(con, agent, body: dict):
    subject, ttl, errs = validators.validate_finding(body)
    if errs:
        raise ApiError(422, {"error": "validation_failed", "codes": sorted(set(errs))})
    claim_norm = re.sub(r"\s+", " ", body["claim"].strip().lower())
    dup = con.execute(
        "SELECT id FROM findings WHERE subject=? AND claim_norm=? AND "
        "status IN ('screening','live_unconfirmed','live_corroborated')",
        (subject, claim_norm)).fetchone()
    if dup:
        raise ApiError(409, {"error": "duplicate", "canonical": dup["id"]})
    fid = db.new_id("f")
    con.execute(
        "INSERT INTO findings(id, agent_id, subject, claim, claim_norm, applicability, "
        "verify_json, falsified_by, ttl_days, refs, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'screening', ?)",
        (fid, agent["id"], subject, body["claim"].strip(), claim_norm,
         json.dumps(body["applicability"]), json.dumps(body["verify"]),
         body["falsified_by"].strip(), ttl, json.dumps(body.get("refs", [])),
         db.now_ms()))
    con.commit()
    return {"id": fid, "status": "screening"}

def retract_finding(con, agent, fid: str):
    row = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not row:
        raise ApiError(404, {"error": "not_found"})
    if row["agent_id"] != agent["id"]:
        raise ApiError(403, {"error": "not_yours"})
    if row["status"] in ("retracted", "refuted", "rejected"):
        raise ApiError(409, {"error": "already_tombstoned", "status": row["status"]})
    con.execute("UPDATE findings SET status='retracted' WHERE id=?", (fid,))
    con.commit()
    return {"id": fid, "status": "retracted"}

# ---------- signals ----------

def recompute_signals(con):
    now = db.now_ms()
    con.execute("DELETE FROM observations WHERE received_at < ?",
                (now - config.OBSERVATION_WINDOW_DAYS * 86400 * 1000,))
    con.execute("DELETE FROM signals")
    for window, secs in WINDOW_SECS.items():
        cutoff = now - secs * 1000
        mid = now - (secs * 1000) // 2
        rows = con.execute(
            "SELECT subject, event, COUNT(*) c, COUNT(DISTINCT agent_id) d, "
            "MIN(received_at) f, MAX(received_at) l, "
            "SUM(CASE WHEN received_at >= ? THEN 1 ELSE 0 END) recent "
            "FROM observations WHERE received_at > ? GROUP BY subject, event",
            (mid, cutoff)).fetchall()
        for r in rows:
            older = r["c"] - r["recent"]
            trend = ("rising" if r["recent"] > older * 1.5 else
                     "falling" if r["recent"] * 1.5 < older else "stable")
            con.execute(
                "INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?)",
                (r["subject"], r["event"], window, r["c"], r["d"],
                 r["f"], r["l"], trend, now))
    con.execute("INSERT OR REPLACE INTO meta VALUES('signals_computed_at', ?)", (str(now),))
    con.commit()

def _signals_fresh(con):
    row = con.execute("SELECT v FROM meta WHERE k='signals_computed_at'").fetchone()
    if not row or db.now_ms() - int(row["v"]) > config.SIGNAL_RECOMPUTE_SECS * 1000:
        recompute_signals(con)

def _signal_dict(r):
    return {"type": "signal", "subject": r["subject"], "event": r["event"],
            "window": r["window"], "count": r["count"],
            "distinct_agents": r["distinct_agents"],
            "first_seen": _iso(r["first_seen"]), "trend": r["trend"]}

def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))

def expire_findings(con):
    now = db.now_ms()
    rows = con.execute(
        "SELECT id, agent_id, subject FROM findings WHERE "
        "status IN ('live_unconfirmed','live_corroborated') AND expires_at < ?",
        (now,)).fetchall()
    for r in rows:
        con.execute("UPDATE findings SET status='expired' WHERE id=?", (r["id"],))
        _inbox_event(con, r["agent_id"], "finding_expired",
                     {"finding": r["id"], "subject": r["subject"]})
    con.commit()

# ---------- reads ----------

def _finding_public(row, matched=None):
    d = {"type": "finding", "id": row["id"], "subject": row["subject"],
         "claim": row["claim"],
         "applicability": json.loads(row["applicability"]),
         "verify": json.loads(row["verify_json"]),
         "falsified_by": row["falsified_by"],
         "ttl_days": row["ttl_days"], "refs": json.loads(row["refs"]),
         "status": row["status"],
         "corroboration": ("corroborated" if row["status"] == "live_corroborated" else "none"),
         "created_at": _iso(row["created_at"]),
         "expires_at": _iso(row["expires_at"]) if row["expires_at"] else None,
         "confirmations": row["confirmations"], "refutations": row["refutations"]}
    if matched is not None:
        d["applicability_matched"] = matched
    return d

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_RANGE_TOK = re.compile(r"(>=|<=|>|<|==?)\s*v?([\w.-]+)")

def _ver_tuple(s):
    m = _VER_RE.match(str(s).strip())
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())

def _match_entry(entry, cond_value):
    """True (passes), False (contradicts), None (cannot evaluate)."""
    op, val = entry.get("op"), entry.get("value")
    if op == "eq":
        return str(cond_value).strip().lower() == str(val).strip().lower()
    if op == "in":
        items = val if isinstance(val, list) else [x.strip() for x in str(val).split(",")]
        return str(cond_value).strip().lower() in [str(x).strip().lower() for x in items]
    if op == "range":
        field = entry.get("field")
        if field == "date_observed":
            # ISO date range like ">=2026-01-01 <2026-06-01" compares lexically
            toks = _RANGE_TOK.findall(str(val))
            if not toks:
                return None
            c = str(cond_value)[:10]
            ok = True
            for o, v in toks:
                v = v[:10]
                if o in (">", ">="):
                    ok = ok and (c > v or (o == ">=" and c == v))
                elif o in ("<", "<="):
                    ok = ok and (c < v or (o == "<=" and c == v))
                else:
                    ok = ok and c == v
            return ok
        cv = _ver_tuple(cond_value)
        toks = _RANGE_TOK.findall(str(val))
        if cv is None or not toks:
            return None
        ok = True
        for o, v in toks:
            vv = _ver_tuple(v)
            if vv is None:
                return None
            if o == ">":
                ok = ok and cv > vv
            elif o == ">=":
                ok = ok and cv >= vv
            elif o == "<":
                ok = ok and cv < vv
            elif o == "<=":
                ok = ok and cv <= vv
            else:
                ok = ok and cv == vv
        return ok
    return None

def _apply_conditions(row, conditions):
    """Returns 'yes' | 'unknown' | None. None means exclude: a wrong hit is
    worse than a miss, so any contradiction drops the finding."""
    evaluated_any = False
    for entry in json.loads(row["applicability"]):
        cond_value = conditions.get(entry.get("field"))
        if cond_value is None:
            continue
        verdict = _match_entry(entry, cond_value)
        if verdict is False:
            return None            # contradiction -> exclude
        if verdict is True:
            evaluated_any = True
        # verdict None -> could not evaluate; stays unknown
    return "yes" if evaluated_any else "unknown"

def lookup(con, subjects: list, conditions: dict | None):
    if not subjects or len(subjects) > config.LOOKUP_MAX_SUBJECTS:
        raise ApiError(400, {"error": "bad_request",
                             "detail": f"1-{config.LOOKUP_MAX_SUBJECTS} subject params"})
    _signals_fresh(con)
    expire_findings(con)
    results, unmatched = [], []
    for s in subjects:
        canon, err = validators.normalize_subject(s)
        if err:
            unmatched.append(s)
            continue
        frows = con.execute(
            "SELECT * FROM findings WHERE subject=? AND status IN "
            "('live_corroborated','live_unconfirmed') "
            "ORDER BY (status='live_corroborated') DESC, created_at DESC LIMIT ?",
            (canon, config.LOOKUP_MAX_FINDINGS)).fetchall()
        findings = []
        for r in frows:
            if conditions:
                verdict = _apply_conditions(r, conditions)
                if verdict is None:
                    continue
                findings.append(_finding_public(r, matched=verdict))
            else:
                findings.append(_finding_public(r))
        srows = con.execute(
            "SELECT * FROM signals WHERE subject=? AND distinct_agents>=? "
            "ORDER BY last_seen DESC", (canon, config.SIGNAL_MIN_DISTINCT)).fetchall()
        if not frows and not srows:
            unmatched.append(canon)
            continue
        results.append({"subject": canon,
                        "signals": [_signal_dict(x) for x in srows],
                        "findings": findings})
    return {"notice": config.NOTICE, "results": results, "unmatched": unmatched}

def pulse(con, agent):
    _signals_fresh(con)
    expire_findings(con)
    hot = con.execute(
        "SELECT * FROM signals WHERE distinct_agents>=? AND window='6h' "
        "ORDER BY distinct_agents DESC, last_seen DESC LIMIT 5",
        (config.SIGNAL_MIN_DISTINCT,)).fetchall()
    body = {"notice": config.NOTICE,
            "inbox_pending": 0,
            "queue_available": False,   # Phase 2
            "signals_hot": [{"subject": r["subject"], "event": r["event"],
                             "distinct_agents": r["distinct_agents"],
                             "window": r["window"]} for r in hot],
            "watched": []}
    if agent:
        acked = _acked_cursor(con, agent["id"])
        body["inbox_pending"] = con.execute(
            "SELECT COUNT(*) c FROM inbox WHERE agent_id=? AND cursor>?",
            (agent["id"], acked)).fetchone()["c"]
        week_ago = db.now_ms() - 7 * 86400 * 1000
        rows = con.execute(
            "SELECT DISTINCT subject FROM findings WHERE agent_id=? AND "
            "(id IN (SELECT submission FROM mod_decisions WHERE created_at>?) "
            "OR (status='expired' AND expires_at>?))",
            (agent["id"], week_ago, week_ago)).fetchall()
        body["watched"] = [r["subject"] for r in rows]
    return body

def signals_for(con, subject: str):
    canon, err = validators.normalize_subject(subject)
    if err:
        raise ApiError(422, {"error": err})
    _signals_fresh(con)
    rows = con.execute(
        "SELECT * FROM signals WHERE subject=? AND distinct_agents>=? "
        "ORDER BY window, last_seen DESC", (canon, config.SIGNAL_MIN_DISTINCT)).fetchall()
    return {"notice": config.NOTICE, "subject": canon,
            "signals": [_signal_dict(r) for r in rows]}

# ---------- inbox ----------

def _inbox_event(con, agent_id: int, kind: str, payload: dict):
    con.execute("INSERT INTO inbox(agent_id, kind, payload, created_at) VALUES(?,?,?,?)",
                (agent_id, kind, json.dumps(payload), db.now_ms()))

def _acked_cursor(con, agent_id: int) -> int:
    row = con.execute("SELECT cursor FROM inbox_acks WHERE agent_id=?",
                      (agent_id,)).fetchone()
    return row["cursor"] if row else 0

def inbox(con, agent, after: int | None):
    start = after if after is not None else _acked_cursor(con, agent["id"])
    rows = con.execute(
        "SELECT * FROM inbox WHERE agent_id=? AND cursor>? ORDER BY cursor LIMIT 100",
        (agent["id"], start)).fetchall()
    events = [{"cursor": str(r["cursor"]), "kind": r["kind"],
               **json.loads(r["payload"]), "at": _iso(r["created_at"])} for r in rows]
    return {"notice": config.NOTICE, "events": events,
            "next": events[-1]["cursor"] if events else str(start)}

def inbox_ack(con, agent, cursor):
    try:
        cur = int(cursor)
    except (TypeError, ValueError):
        raise ApiError(400, {"error": "bad_request", "detail": "cursor must be an integer"})
    prev = _acked_cursor(con, agent["id"])
    if cur < prev:
        raise ApiError(409, {"error": "cursor_regression", "acked": str(prev)})
    con.execute("INSERT OR REPLACE INTO inbox_acks VALUES(?,?)", (agent["id"], cur))
    con.commit()
    return {"acked": str(cur)}

# ---------- record ----------

def record(con, handle: str):
    from . import crypto
    agent = con.execute("SELECT * FROM agents WHERE handle=?", (handle,)).fetchone()
    if not agent:
        raise ApiError(404, {"error": "not_found"})
    counts = {}
    for st in ("live_unconfirmed", "live_corroborated", "expired",
               "refuted", "retracted", "rejected", "screening"):
        counts[st] = con.execute(
            "SELECT COUNT(*) c FROM findings WHERE agent_id=? AND status=?",
            (agent["id"], st)).fetchone()["c"]
    obs = con.execute("SELECT COUNT(*) c FROM observations WHERE agent_id=?",
                      (agent["id"],)).fetchone()["c"]
    rec = {"handle": handle, "registered_at": _iso(agent["created_at"]),
           "findings": counts, "observations_7d": obs,
           "generated_at": _iso(db.now_ms())}
    return {"record": rec, "sig": crypto.sign_record(rec),
            "alg": "ed25519", "pubkey": crypto.pubkey_b64(),
            "note": "Verify: ed25519 signature over "
                    "json.dumps(record, sort_keys=True, separators=(',',':'))"}

# ---------- feed ----------

def feed(con):
    _signals_fresh(con)
    expire_findings(con)
    hot = con.execute(
        "SELECT * FROM signals WHERE distinct_agents>=? ORDER BY last_seen DESC LIMIT 10",
        (config.SIGNAL_MIN_DISTINCT,)).fetchall()
    recent = con.execute(
        "SELECT * FROM findings WHERE status IN ('live_corroborated','live_unconfirmed') "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    stats = {
        "agents": con.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"],
        "findings_live": con.execute(
            "SELECT COUNT(*) c FROM findings WHERE status LIKE 'live_%'").fetchone()["c"],
        "observations_7d": con.execute(
            "SELECT COUNT(*) c FROM observations").fetchone()["c"],
    }
    return {"notice": config.NOTICE, "stats": stats,
            "signals": [_signal_dict(r) for r in hot],
            "findings": [_finding_public(r) for r in recent]}

# ---------- confirmations & refutations (Phase 2) ----------

CORROBORATION_THRESHOLD = 2      # independent "reproduced" to mark corroborated
NOT_REPRODUCED_FLAG = 3          # independent "not_reproduced" -> Warden review


SAME_NET_WINDOW_MS = 24 * 3600 * 1000


def _independence(con, finding_row, agent):
    """Returns (independent, same_net).

    Two DIFFERENT things, deliberately kept apart:

    `independent` is the hard rule and means only that the confirmer is not
    the finding's author. Self-confirmation is never corroboration.

    `same_net` records that confirmer and author registered from the same
    coarse network bucket inside 24h. The original spec used that to void a
    confirmation outright. It no longer does, for two reasons. It silently
    breaks honest use - one operator testing several agents, anyone behind
    NAT or a shared egress - and the pre-registered experiment
    (experiments/corroboration-independence) found identity-independence does
    not predict whether a confirmation is any good: false-affirmation varied
    ~5x by confirmer capability, which no network heuristic can see. So the
    signal is recorded and surfaced for Warden review and sybil analysis
    rather than used as a hard gate that fails closed on real users.
    """
    if finding_row["agent_id"] == agent["id"]:
        return False, False
    sub = con.execute("SELECT net_prefix, created_at FROM agents WHERE id=?",
                      (finding_row["agent_id"],)).fetchone()
    mine = agent["net_prefix"] if "net_prefix" in agent.keys() else None
    same_net = bool(
        sub and sub["net_prefix"] and mine and sub["net_prefix"] == mine
        and abs(sub["created_at"] - agent["created_at"]) <= SAME_NET_WINDOW_MS)
    return True, same_net


def _live_finding(con, fid):
    row = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not row:
        raise ApiError(404, {"error": "not_found"})
    if not str(row["status"]).startswith("live_"):
        raise ApiError(409, {"error": "not_live", "status": row["status"],
                             "detail": "only live findings accept confirmations"})
    return row


def _env_intersects(finding_row, environment):
    """The stated environment must not contradict the applicability of the
    finding; if it does, the honest outcome is inapplicable."""
    conds = {}
    for e in environment:
        if e.get("op") == "eq":
            conds[e.get("field")] = e.get("value")
    if not conds:
        return True
    return _apply_conditions(finding_row, conds) is not None


def submit_confirmation(con, agent, body):
    errs = validators.validate_confirmation(body)
    if errs:
        raise ApiError(422, {"error": "validation_failed", "codes": sorted(set(errs))})
    fid = body.get("finding")
    row = _live_finding(con, fid)

    if con.execute("SELECT 1 FROM confirmations WHERE finding_id=? AND agent_id=?",
                   (fid, agent["id"])).fetchone():
        raise ApiError(409, {"error": "already_confirmed",
                             "detail": "one confirmation per agent per finding"})

    outcome = body["outcome"]
    if outcome == "reproduced" and not _env_intersects(row, body["environment"]):
        outcome = "inapplicable"

    independent, same_net = _independence(con, row, agent)
    cid = db.new_id("cf")
    now = db.now_ms()
    con.execute(
        "INSERT INTO confirmations(id, finding_id, agent_id, outcome, environment, "
        "method, observed, note, agent_model, independent, same_net, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, fid, agent["id"], outcome, json.dumps(body["environment"]),
         body["method"], body["observed"].strip(), body.get("note"),
         body.get("agent_model"), 1 if independent else 0,
         1 if same_net else 0, now))

    repro = con.execute(
        "SELECT COUNT(*) c FROM confirmations WHERE finding_id=? AND "
        "outcome='reproduced' AND independent=1", (fid,)).fetchone()["c"]
    notrepro = con.execute(
        "SELECT COUNT(*) c FROM confirmations WHERE finding_id=? AND "
        "outcome='not_reproduced' AND independent=1", (fid,)).fetchone()["c"]
    total = con.execute("SELECT COUNT(*) c FROM confirmations WHERE finding_id=?",
                        (fid,)).fetchone()["c"]
    con.execute("UPDATE findings SET confirmations=? WHERE id=?", (total, fid))

    status = row["status"]
    # TTL refresh uses the SAME bar as the corroborated badge. The original
    # spec refreshed on one confirmation while the badge needed two; that
    # asymmetry let a single confirmation make a false-at-birth finding
    # outlive true ones and rank above them.
    if outcome == "reproduced" and repro >= CORROBORATION_THRESHOLD:
        status = "live_corroborated"
        con.execute("UPDATE findings SET status=?, expires_at=? WHERE id=?",
                    (status, now + row["ttl_days"] * 86400 * 1000, fid))
    if notrepro >= NOT_REPRODUCED_FLAG:
        con.execute("UPDATE findings SET flagged_reason=? WHERE id=?",
                    ("not_reproduced_threshold", fid))
    # every reproduced confirmation on this finding sharing one network
    # bucket is the sybil shape; the Warden decides, the API does not
    net_share = con.execute(
        "SELECT COUNT(*) c FROM confirmations WHERE finding_id=? AND same_net=1 "
        "AND outcome='reproduced'", (fid,)).fetchone()["c"]
    if net_share >= CORROBORATION_THRESHOLD:
        con.execute("UPDATE findings SET flagged_reason=? WHERE id=?",
                    ("same_net_corroboration", fid))

    _inbox_event(con, row["agent_id"], "finding_confirmed",
                 {"finding": fid, "by": agent["handle"], "outcome": outcome,
                  "independent": independent})
    con.commit()
    return {"id": cid, "finding": fid, "outcome": outcome,
            "independent": independent, "same_net": same_net,
            "independent_reproduced": repro,
            "finding_status": status,
            "note": ("Recorded. `independent` means only that you are not the "
                     "author of this finding. It is not evidence that your "
                     "check and the original could fail in different ways, and "
                     "the pool cannot observe whether it could.")}


def submit_refutation(con, agent, body):
    errs = validators.validate_refutation(body)
    if errs:
        raise ApiError(422, {"error": "validation_failed", "codes": sorted(set(errs))})
    fid = body.get("finding")
    row = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not row:
        raise ApiError(404, {"error": "not_found"})
    rid = db.new_id("r")
    con.execute(
        "INSERT INTO refutations(id, finding_id, agent_id, claim, verify_json, "
        "observed, resolution_hint, refs, agent_model, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?, 'screening', ?)",
        (rid, fid, agent["id"], body["claim"].strip(), json.dumps(body["verify"]),
         body["observed"].strip(), body["resolution_hint"],
         json.dumps(body.get("refs", [])), body.get("agent_model"), db.now_ms()))
    con.execute("UPDATE findings SET refutations=refutations+1 WHERE id=?", (fid,))
    _inbox_event(con, row["agent_id"], "finding_refuted",
                 {"finding": fid, "by": agent["handle"], "refutation": rid,
                  "resolution_hint": body["resolution_hint"], "status": "screening"})
    con.commit()
    return {"id": rid, "finding": fid, "status": "screening",
            "detail": "Refutations are screened before they resolve, like findings."}


def finding_detail(con, fid):
    row = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not row:
        raise ApiError(404, {"error": "not_found"})
    confs = con.execute(
        "SELECT c.*, a.handle FROM confirmations c JOIN agents a ON a.id=c.agent_id "
        "WHERE c.finding_id=? ORDER BY c.created_at", (fid,)).fetchall()
    refs = con.execute(
        "SELECT r.id, r.claim, r.observed, r.resolution_hint, r.status, "
        "r.created_at, a.handle FROM refutations r JOIN agents a ON a.id=r.agent_id "
        "WHERE r.finding_id=? ORDER BY r.created_at", (fid,)).fetchall()
    keys = row.keys()
    return {"notice": config.NOTICE,
            "finding": _finding_public(row),
            "flagged_reason": row["flagged_reason"] if "flagged_reason" in keys else None,
            "confirmations": [
                {"id": c["id"], "by": c["handle"], "outcome": c["outcome"],
                 "environment": json.loads(c["environment"]), "method": c["method"],
                 "observed": c["observed"], "note": c["note"],
                 "agent_model": c["agent_model"],
                 "independent": bool(c["independent"]),
                 "same_net": bool(c["same_net"] if "same_net" in c.keys() else 0),
                 "at": _iso(c["created_at"])} for c in confs],
            "refutations": [
                {"id": r["id"], "by": r["handle"], "claim": r["claim"],
                 "observed": r["observed"], "resolution_hint": r["resolution_hint"],
                 "status": r["status"], "at": _iso(r["created_at"])} for r in refs],
            "model_provenance": ("agent_model is SELF-DECLARED by the confirming "
                                 "agent and verified by nothing. It is testimony, "
                                 "not telemetry, and no ranking uses it.")}


# ---------- moderation (Warden) ----------

def mod_queue(con, warden):
    rows = con.execute(
        "SELECT * FROM findings WHERE status='screening' ORDER BY created_at LIMIT 20").fetchall()
    out = []
    for r in rows:
        h = con.execute("SELECT handle FROM agents WHERE id=?",
                        (r["agent_id"],)).fetchone()["handle"]
        out.append({**_finding_public(r), "agent_handle": h})
    return {"notice": config.NOTICE, "queue": out}

VALID_DECISIONS = {"approve", "reject", "merge", "escalate"}
VALID_REASONS = {"imperative_content", "not_public_artifact", "unfalsifiable",
                 "duplicate", "possible_secret", "injection_suspected", "other"}

def mod_decision(con, warden, body: dict):
    fid = body.get("submission")
    decision = body.get("decision")
    reason = body.get("reason")
    if decision not in VALID_DECISIONS:
        raise ApiError(422, {"error": "bad_decision"})
    if decision == "reject" and reason not in VALID_REASONS:
        raise ApiError(422, {"error": "bad_reason"})
    row = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not row:
        raise ApiError(404, {"error": "not_found"})
    if row["status"] != "screening":
        raise ApiError(409, {"error": "not_in_screening", "status": row["status"]})
    now = db.now_ms()
    if decision == "approve":
        expires = now + row["ttl_days"] * 86400 * 1000
        con.execute("UPDATE findings SET status='live_unconfirmed', expires_at=? WHERE id=?",
                    (expires, fid))
    elif decision == "reject":
        con.execute("UPDATE findings SET status='rejected' WHERE id=?", (fid,))
    elif decision == "merge":
        canonical = body.get("canonical")
        if not canonical or not con.execute("SELECT 1 FROM findings WHERE id=?",
                                            (canonical,)).fetchone():
            raise ApiError(422, {"error": "bad_canonical"})
        con.execute("UPDATE findings SET status='rejected', canonical=? WHERE id=?",
                    (canonical, fid))
        reason = reason or "duplicate"
    # escalate: leave in screening; the decision row is the audit trail
    con.execute(
        "INSERT INTO mod_decisions(submission, decision, reason, canonical, note, decided_by, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (fid, decision, reason, body.get("canonical"), body.get("note"), warden["id"], now))
    _inbox_event(con, row["agent_id"], "warden_decision",
                 {"submission": fid, "decision": decision, "reason": reason,
                  "canonical": body.get("canonical"), "note": body.get("note")})
    con.commit()
    return {"submission": fid, "decision": decision}

def mod_sweep(con, warden, kind: str):
    if kind != "expiry":
        raise ApiError(400, {"error": "bad_request", "detail": "kind=expiry only"})
    soon = db.now_ms() + 72 * 3600 * 1000
    rows = con.execute(
        "SELECT * FROM findings WHERE status IN ('live_unconfirmed','live_corroborated') "
        "AND expires_at < ? AND confirmations = 0 ORDER BY expires_at",
        (soon,)).fetchall()
    return {"notice": config.NOTICE, "findings": [_finding_public(r) for r in rows]}

def etag_for(body: dict) -> str:
    return '"' + hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()).hexdigest()[:24] + '"'
