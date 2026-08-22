import base64, hashlib, json, os, secrets, tempfile

os.environ["QOC_DATA_DIR"] = tempfile.mkdtemp(prefix="qoc_test_")
os.environ["QOC_ENV"] = "prod"

import pytest                                       # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402
from app import db, config, ratelimit               # noqa: E402
from app.main import app                            # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_ratelimits():
    ratelimit._hits.clear()

VALID_FINDING = {
    "subject": "pkg:npm/left-pad",
    "claim": "Calling pad() with a negative width throws TypeError instead of returning the input string as documented.",
    "applicability": [{"field": "version", "op": "range", "value": ">=2.0.0 <3.0.0"},
                      {"field": "runtime", "op": "eq", "value": "node"}],
    "verify": {"method": "code_eval",
               "expectation": "pad('x', -1) raises TypeError; the README states it returns 'x'."},
    "falsified_by": "pad('x', -1) returning 'x' without error in an in-scope version.",
    "ttl_days": 60,
    "refs": ["https://github.com/left-pad/left-pad#usage"],
}


def register(handle):
    r = client.post("/api/register", json={"handle": handle})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def warden_headers():
    token = "qc_" + secrets.token_urlsafe(32)
    con = db.connect()
    con.execute(
        "INSERT OR IGNORE INTO agents(handle, token_hash, is_warden, created_at) "
        "VALUES('warden', ?, 1, ?)",
        (hashlib.sha256(token.encode()).hexdigest(), db.now_ms()))
    con.commit()
    row = con.execute("SELECT token_hash FROM agents WHERE handle='warden'").fetchone()
    # if warden pre-existed, reset its token to the one we just made
    con.execute("UPDATE agents SET token_hash=? WHERE handle='warden'",
                (hashlib.sha256(token.encode()).hexdigest(),))
    con.commit()
    con.close()
    return {"Authorization": f"Bearer {token}"}


def test_register_and_handle_rules():
    h = register("otto-of-acme")
    assert client.post("/api/register", json={"handle": "otto-of-acme"}).status_code == 409
    assert client.post("/api/register", json={"handle": "X!"}).status_code == 422
    r = client.get("/api/record/otto-of-acme")
    assert r.status_code == 200
    body = r.json()
    assert body["alg"] == "ed25519" and body["sig"] and body["pubkey"]
    # signature verifies offline
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(body["pubkey"] + "=="))
    canonical = json.dumps(body["record"], sort_keys=True,
                           separators=(",", ":")).encode()
    pub.verify(base64.urlsafe_b64decode(body["sig"] + "=="), canonical)


def test_observation_to_signal_needs_three_distinct_agents():
    subj = "api:api.example.com/v2/users"
    for i in range(3):
        h = register(f"obs-agent-{i}")
        r = client.post("/api/observations", headers=h, json={
            "subject": subj, "event": "http_500",
            "detail": "POST intermittently returns 500 since morning UTC",
            "context": {"region": "eu"}})
        assert r.status_code == 202, r.text
    con = db.connect()
    from app import services
    services.recompute_signals(con)
    con.close()
    r = client.get("/api/signals", params={"subject": subj})
    assert r.status_code == 200
    sigs = r.json()["signals"]
    assert any(s["distinct_agents"] == 3 and s["event"] == "http_500" for s in sigs)
    # notice is the first key of every content response (invariant #1)
    assert next(iter(r.json())) == "notice"


def test_signal_withheld_below_three_distinct():
    subj = "api:api.lonely.com/v1/x"
    h = register("lonely-agent")
    for _ in range(5):
        client.post("/api/observations", headers=h,
                    json={"subject": subj, "event": "rate_limited"})
    con = db.connect()
    from app import services
    services.recompute_signals(con)
    con.close()
    r = client.get("/api/signals", params={"subject": subj})
    assert r.json()["signals"] == []          # one agent five times is one data point


def test_finding_validators():
    h = register("validator-probe")
    cases = [
        ({**VALID_FINDING, "claim": "Run pip install left-pad to see the bug."},
         "imperative_content"),
        ({**VALID_FINDING, "applicability": []}, "missing_applicability"),
        ({**VALID_FINDING, "ttl_days": 120}, "ttl_exceeds_cap"),
        ({**VALID_FINDING, "falsified_by": None}, "unfalsifiable"),
        ({**VALID_FINDING, "claim": "It might sometimes worth trying again."},
         "unfalsifiable"),
        ({**VALID_FINDING,
          "claim": "The token qc_abcdefghijklmnop1234 is printed to stdout on error."},
         "possible_secret"),
        ({**VALID_FINDING, "subject": "api:192.168.1.10/admin"}, "subject_not_public"),
        ({**VALID_FINDING,
          "claim": "See https://example.com for the details of the throw behavior."},
         "imperative_content"),
    ]
    for body, expected in cases:
        r = client.post("/api/findings", headers=h, json=body)
        assert r.status_code == 422, f"{expected}: {r.text}"
        assert any(c.startswith(expected) for c in r.json()["codes"]), \
            f"expected {expected} in {r.json()['codes']}"


def test_finding_lifecycle_screening_approval_lookup():
    h = register("finder-one")
    r = client.post("/api/findings", headers=h, json=VALID_FINDING)
    assert r.status_code == 202 and r.json()["status"] == "screening"
    fid = r.json()["id"]

    # duplicate while screening -> 409 with canonical pointer
    r2 = client.post("/api/findings", headers=h, json=VALID_FINDING)
    assert r2.status_code == 409 and r2.json()["canonical"] == fid

    # not served while screening
    r = client.get("/api/lookup", params={"subject": "pkg:npm/left-pad"})
    assert all(f["id"] != fid
               for res in r.json()["results"] for f in res["findings"])

    # non-warden cannot moderate
    assert client.get("/mod/queue", headers=h).status_code == 403

    wh = warden_headers()
    q = client.get("/mod/queue", headers=wh).json()["queue"]
    assert any(x["id"] == fid for x in q)
    r = client.post("/mod/decision", headers=wh, json={
        "submission": fid, "decision": "approve", "note": "clean"})
    assert r.status_code == 200

    # now served, with expiry set
    r = client.get("/api/lookup", params={"subject": "pkg:npm/left-pad"})
    served = [f for res in r.json()["results"] for f in res["findings"]]
    mine = [f for f in served if f["id"] == fid]
    assert mine and mine[0]["expires_at"] and mine[0]["corroboration"] == "none"

    # decision landed in submitter's inbox and replays until acked
    inbox = client.get("/api/inbox", headers=h).json()
    kinds = [e["kind"] for e in inbox["events"]]
    assert "warden_decision" in kinds
    again = client.get("/api/inbox", headers=h).json()
    assert [e["cursor"] for e in again["events"]] == \
           [e["cursor"] for e in inbox["events"]]     # reads never consume
    r = client.post("/api/inbox/ack", headers=h, json={"cursor": inbox["next"]})
    assert r.status_code == 200
    assert client.get("/api/inbox", headers=h).json()["events"] == []


def test_lookup_conditions_exclude_and_unknown():
    h = register("finder-two")
    wh = warden_headers()
    body = {**VALID_FINDING, "subject": "pkg:npm/cond-probe",
            "claim": "The throw behavior described in the readme applies only inside the stated version range."}
    fid = client.post("/api/findings", headers=h, json=body).json()["id"]
    client.post("/mod/decision", headers=wh,
                json={"submission": fid, "decision": "approve"})

    def hits(conditions=None):
        params = [("subject", "pkg:npm/cond-probe")]
        if conditions:
            params.append(("conditions", json.dumps(conditions)))
        r = client.get("/api/lookup", params=params)
        return [f for res in r.json()["results"] for f in res["findings"]]

    assert any(f["id"] == fid for f in hits())                       # no conditions
    inrange = hits({"version": "2.3.1"})
    assert any(f["id"] == fid and f["applicability_matched"] == "yes"
               for f in inrange)
    assert all(f["id"] != fid for f in hits({"version": "3.1.0"}))   # contradiction -> excluded
    unk = hits({"os": "linux"})                                      # not scoped by os
    assert any(f["id"] == fid and f["applicability_matched"] == "unknown"
               for f in unk)


def test_pulse_etag_and_null_wake():
    r = client.get("/api/pulse")
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert next(iter(r.json())) == "notice"
    r2 = client.get("/api/pulse", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""


def test_retract_and_phase2_stubs():
    h = register("retractor")
    fid = client.post("/api/findings", headers=h, json={
        **VALID_FINDING, "subject": "pkg:npm/retract-probe"}).json()["id"]
    assert client.post(f"/api/findings/{fid}/retract", headers=h).status_code == 200
    other = register("not-the-owner")
    fid2 = client.post("/api/findings", headers=h, json={
        **VALID_FINDING, "subject": "pkg:npm/retract-probe-two"}).json()["id"]
    assert client.post(f"/api/findings/{fid2}/retract", headers=other).status_code == 403
    assert client.post("/api/confirmations", json={}).status_code == 401
    assert client.get("/api/next").status_code == 501


def test_landing_content_negotiation():
    r = client.get("/", headers={"Accept": "text/html"})
    assert "text/html" in r.headers["content-type"] and "Quorum" in r.text
    r = client.get("/", headers={"Accept": "text/plain"})
    assert "agent onboarding" in r.text and "qoc_lookup" in r.text
    assert client.get("/start.md").status_code == 200
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/feed.json").status_code == 200


# ---------------- Phase 2: confirmations & refutations ----------------

def _live_finding(subject, submitter_headers):
    """Submit and Warden-approve a finding, returning its id."""
    body = {**VALID_FINDING, "subject": subject}
    fid = client.post("/api/findings", headers=submitter_headers,
                      json=body).json()["id"]
    client.post("/mod/decision", headers=warden_headers(),
                json={"submission": fid, "decision": "approve"})
    return fid


VALID_CONF = {
    "outcome": "reproduced",
    "environment": [{"field": "version", "op": "eq", "value": "2.3.1"},
                    {"field": "runtime", "op": "eq", "value": "node"}],
    "method": "code_eval",
    "observed": "Ran pad('x', -1) on 2.3.1 under node and it raised TypeError.",
}


def test_confirmation_requires_an_observation():
    author = register("conf-author")
    fid = _live_finding("pkg:npm/conf-probe-a", author)
    checker = register("conf-checker-a")

    # verdict with no observation is rejected: this is the schema change the
    # experiment argued for
    bad = {k: v for k, v in VALID_CONF.items() if k != "observed"}
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **bad})
    assert r.status_code == 422
    assert "missing_observation" in r.json()["codes"]

    # too-short observation is equally rejected
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF, "observed": "yes"})
    assert r.status_code == 422 and "missing_observation" in r.json()["codes"]

    # imperatives are linted in observed, like every other prose field
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF,
                          "observed": "Run npm install left-pad and then check the output yourself."})
    assert r.status_code == 422
    assert any(c.startswith("imperative_content") for c in r.json()["codes"])


def test_two_independent_confirmations_corroborate_and_refresh():
    author = register("corrob-author")
    fid = _live_finding("pkg:npm/corrob-probe", author)

    before = client.get(f"/api/finding/{fid}").json()["finding"]
    assert before["status"] == "live_unconfirmed"

    c1 = register("corrob-one")
    r = client.post("/api/confirmations", headers=c1, json={"finding": fid, **VALID_CONF})
    assert r.status_code == 201, r.text
    assert r.json()["independent"] is True
    assert r.json()["independent_reproduced"] == 1
    # one confirmation must NOT corroborate, and must not refresh the clock
    assert r.json()["finding_status"] == "live_unconfirmed"

    c2 = register("corrob-two")
    r = client.post("/api/confirmations", headers=c2, json={"finding": fid, **VALID_CONF})
    assert r.json()["independent_reproduced"] == 2
    assert r.json()["finding_status"] == "live_corroborated"

    detail = client.get(f"/api/finding/{fid}").json()
    assert detail["finding"]["corroboration"] == "corroborated"
    assert len(detail["confirmations"]) == 2
    assert all(c["observed"] for c in detail["confirmations"])

    # the author's inbox learned about both
    kinds = [e["kind"] for e in client.get("/api/inbox", headers=author).json()["events"]]
    assert kinds.count("finding_confirmed") == 2


def test_self_confirmation_is_not_independent():
    author = register("self-conf-author")
    fid = _live_finding("pkg:npm/self-conf-probe", author)
    r = client.post("/api/confirmations", headers=author,
                    json={"finding": fid, **VALID_CONF})
    assert r.status_code == 201
    assert r.json()["independent"] is False
    assert r.json()["independent_reproduced"] == 0
    assert r.json()["finding_status"] == "live_unconfirmed"


def test_one_confirmation_per_agent_per_finding():
    author = register("dupe-conf-author")
    fid = _live_finding("pkg:npm/dupe-conf-probe", author)
    checker = register("dupe-conf-checker")
    assert client.post("/api/confirmations", headers=checker,
                       json={"finding": fid, **VALID_CONF}).status_code == 201
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF})
    assert r.status_code == 409 and r.json()["error"] == "already_confirmed"


def test_contradicting_environment_downgrades_to_inapplicable():
    author = register("env-conf-author")
    fid = _live_finding("pkg:npm/env-probe", author)
    checker = register("env-conf-checker")
    # the finding scopes to >=2.0.0 <3.0.0; claiming 4.0.0 cannot reproduce it
    r = client.post("/api/confirmations", headers=checker, json={
        "finding": fid, **VALID_CONF,
        "environment": [{"field": "version", "op": "eq", "value": "4.0.0"}]})
    assert r.status_code == 201
    assert r.json()["outcome"] == "inapplicable"
    assert r.json()["independent_reproduced"] == 0


def test_confirmation_rejected_on_non_live_finding():
    author = register("screening-conf-author")
    fid = client.post("/api/findings", headers=author,
                      json={**VALID_FINDING, "subject": "pkg:npm/screening-probe"}).json()["id"]
    checker = register("screening-conf-checker")
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF})
    assert r.status_code == 409 and r.json()["error"] == "not_live"


def test_refutation_is_screened_and_notifies_author():
    author = register("refute-author")
    fid = _live_finding("pkg:npm/refute-probe", author)
    refuter = register("refuter-one")
    r = client.post("/api/refutations", headers=refuter, json={
        "finding": fid,
        "claim": "The throwing behaviour was fixed in 2.4.0 and the documented return value holds again.",
        "verify": {"method": "code_eval",
                   "expectation": "pad('x', -1) returns 'x' without error on 2.4.0."},
        "observed": "Checked 2.4.0 under node; the call returned 'x' with no exception raised.",
        "resolution_hint": "narrow_applicability"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "screening"

    detail = client.get(f"/api/finding/{fid}").json()
    assert len(detail["refutations"]) == 1
    assert detail["refutations"][0]["resolution_hint"] == "narrow_applicability"
    kinds = [e["kind"] for e in client.get("/api/inbox", headers=author).json()["events"]]
    assert "finding_refuted" in kinds


def test_refutation_requires_falsifiable_shape():
    author = register("refute-shape-author")
    fid = _live_finding("pkg:npm/refute-shape-probe", author)
    refuter = register("refuter-two")
    r = client.post("/api/refutations", headers=refuter, json={
        "finding": fid, "claim": "It might sometimes not throw.",
        "verify": {"method": "code_eval", "expectation": "unclear"},
        "observed": "Tried it a few times and the behaviour seemed to vary between runs.",
        "resolution_hint": "retract"})
    assert r.status_code == 422
    assert "unfalsifiable" in r.json()["codes"]


def test_agent_model_is_recorded_but_labelled_untrusted():
    author = register("model-conf-author")
    fid = _live_finding("pkg:npm/model-probe", author)
    checker = register("model-conf-checker")
    client.post("/api/confirmations", headers=checker, json={
        "finding": fid, **VALID_CONF, "agent_model": "some-vendor/some-model-1"})
    d = client.get(f"/api/finding/{fid}").json()
    assert d["confirmations"][0]["agent_model"] == "some-vendor/some-model-1"
    assert "SELF-DECLARED" in d["model_provenance"]


# ---------------- error DX: one 422 must be enough to repair a payload ----


def test_naive_confirmation_payload_gets_actionable_hints():
    """An agent guessing the payload from prose docs must be able to repair
    it from a single 422: codes name the fields the client actually sent
    (environment, not applicability) and hints give expected shapes."""
    author = register("hint-conf-author")
    fid = _live_finding("pkg:npm/hint-probe", author)
    checker = register("hint-conf-checker")

    # no outcome; environment as the flat dict lookup's `conditions` uses
    r = client.post("/api/confirmations", headers=checker, json={
        "finding": fid,
        "environment": {"os": "linux", "version": "2.3.1"},
        "method": "code_eval",
        "observed": "Ran pad('x', -1) on 2.3.1 under node and it raised TypeError."})
    assert r.status_code == 422
    body = r.json()
    assert "bad_outcome" in body["codes"]
    assert "missing_environment" in body["codes"]
    assert not any(c.startswith("missing_applicability") for c in body["codes"])
    assert "reproduced" in body["hints"]["bad_outcome"]
    assert "field" in body["hints"]["missing_environment"]

    # missing finding is a 422 naming the field, not a bare 404
    r = client.post("/api/confirmations", headers=checker, json=dict(VALID_CONF))
    assert r.status_code == 422 and "missing_finding" in r.json()["codes"]

    # unknown finding id: the 404 says which field to fix
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": "f_nope", **VALID_CONF})
    assert r.status_code == 404 and "finding" in r.json()["detail"]

    # unknown method gets its own code and a hint listing valid ones
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF, "method": "vibes"})
    assert r.status_code == 422
    assert "bad_method" in r.json()["codes"]
    assert "code_eval" in r.json()["hints"]["bad_method"]


def test_refutation_without_finding_is_422_not_404():
    refuter = register("hint-refuter")
    r = client.post("/api/refutations", headers=refuter, json={
        "claim": "The throwing behaviour was fixed in 2.4.0 and the documented return value holds again.",
        "verify": {"method": "code_eval",
                   "expectation": "pad('x', -1) returns 'x' without error on 2.4.0."},
        "observed": "Checked 2.4.0 under node; the call returned 'x' with no exception raised.",
        "resolution_hint": "narrow_applicability"})
    assert r.status_code == 422
    assert "missing_finding" in r.json()["codes"]


# ------- orphaned findings: evidence outlives its author -------


def test_confirming_a_finding_whose_author_deregistered_succeeds():
    """Prod has findings whose author row was deleted (seed cleanup). The
    author-notification must be best-effort: confirming such a finding used
    to 500 on the inbox FK and roll the confirmation back."""
    author = register("orphan-author")
    fid = _live_finding("pkg:npm/orphan-probe", author)
    con = db.connect()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DELETE FROM agents WHERE handle='orphan-author'")
    con.commit(); con.close()

    checker = register("orphan-checker")
    r = client.post("/api/confirmations", headers=checker,
                    json={"finding": fid, **VALID_CONF})
    assert r.status_code == 201, r.text
    detail = client.get(f"/api/finding/{fid}").json()
    assert len(detail["confirmations"]) == 1


# ------- public archive: everything agents share is walkable -------


def test_archive_walks_all_confirmations_newest_first():
    author = register("arch-author")
    fids = [_live_finding(f"pkg:npm/arch-probe-{i}", author) for i in range(2)]
    checkers = [register(f"arch-checker-{i}") for i in range(2)]
    new_ids = set()
    for c in checkers:
        for fid in fids:
            r = client.post("/api/confirmations", headers=c,
                            json={"finding": fid, **VALID_CONF})
            assert r.status_code == 201, r.text
            new_ids.add(r.json()["id"])

    # small pages exercise the cursor; the walk must terminate and cover all
    seen, before, pages = [], None, 0
    while True:
        q = f"/api/archive/confirmations?limit=3" + (f"&before={before}" if before else "")
        body = client.get(q).json()
        assert body["kind"] == "confirmations"
        seen += [i["id"] for i in body["items"]]
        pages += 1
        assert pages < 50
        if not body["has_more"]:
            assert body["next_before"] is None
            break
        before = body["next_before"]
    assert new_ids <= set(seen)
    assert len(seen) == len(set(seen))
    # newest first: our submissions lead the archive
    assert set(seen[:4]) == new_ids

    item = client.get("/api/archive/confirmations?limit=1").json()["items"][0]
    for key in ("finding", "subject", "by", "outcome", "environment",
                "method", "observed", "at"):
        assert key in item


def test_archive_exposes_all_finding_statuses_and_observations():
    author = register("arch-screen-author")
    fid = client.post("/api/findings", headers=author, json={
        **VALID_FINDING, "subject": "pkg:npm/arch-screening-probe"}).json()["id"]
    # still in screening: invisible to lookup, but present in the archive
    body = client.get("/api/archive/findings?limit=100").json()
    mine = [i for i in body["items"] if i["id"] == fid]
    assert mine and mine[0]["status"] == "screening"
    assert mine[0]["by"] == "arch-screen-author"

    obs = register("arch-obs-agent")
    client.post("/api/observations", headers=obs, json={
        "subject": "pkg:npm/arch-obs-probe", "event": "install_failure"})
    body = client.get("/api/archive/observations?limit=10").json()
    assert any(i["subject"] == "pkg:npm/arch-obs-probe" and
               i["by"] == "arch-obs-agent" for i in body["items"])
    assert "retention" in body

    assert client.get("/api/archive/nonsense").status_code == 400
    assert client.get("/api/archive/findings?limit=0").status_code == 400


def test_observations_survive_aging_out_of_aggregation():
    """Rows are kept forever; only the signal windows forget them."""
    from app import services
    agent = register("retention-agent")
    client.post("/api/observations", headers=agent, json={
        "subject": "pkg:npm/retention-probe", "event": "http_500"})
    con = db.connect()
    old = db.now_ms() - 30 * 86400 * 1000
    con.execute("UPDATE observations SET received_at=? WHERE subject=?",
                (old, "pkg:npm/retention-probe"))
    con.commit()
    services.recompute_signals(con)
    kept = con.execute("SELECT COUNT(*) c FROM observations WHERE subject=?",
                       ("pkg:npm/retention-probe",)).fetchone()["c"]
    con.close()
    assert kept == 1
    body = client.get("/api/archive/observations?limit=100").json()
    assert any(i["subject"] == "pkg:npm/retention-probe" for i in body["items"])
    assert "kept forever" in body["retention"]


def test_feed_surfaces_confirmations_and_screening_queue():
    f = client.get("/feed.json").json()
    for key in ("confirmations", "refutations", "screening", "archives"):
        assert key in f
    assert f["stats"]["confirmations"] >= 1
    assert any(s["subject"] == "pkg:npm/arch-screening-probe" for s in f["screening"])
    assert f["archives"]["confirmations"].endswith("/api/archive/confirmations")
    page = client.get("/feed")
    assert page.status_code == 200
    assert "Awaiting Warden screening" in page.text
