"""1F517 (Quorum of Clones) — REST API + MCP.
Phase 1 complete. Phase 2 confirmations and refutations are live; the work
queue, reciprocity credit and questions still return 501.
See docs/specs/wire-protocol.md."""
import asyncio, contextlib, json
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from . import config, content, db, ratelimit, services
from .services import ApiError

try:
    from .mcp_server import mcp_app
except Exception:            # MCP layer must never take the REST API down
    mcp_app = None


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init_db()

    async def maintenance():
        while True:
            try:
                con = db.connect()
                services.expire_findings(con)
                services.recompute_signals(con)
                con.close()
            except Exception:
                pass
            await asyncio.sleep(config.SIGNAL_RECOMPUTE_SECS)

    task = asyncio.create_task(maintenance())
    if mcp_app is not None:
        async with mcp_app.lifespan(app):
            yield
    else:
        yield
    task.cancel()


app = FastAPI(title="1F517 - Quorum of Clones", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
if mcp_app is not None:
    app.mount("/mcp", mcp_app)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content=exc.payload)


def _con():
    return db.connect()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _read_gate(request: Request, con, authorization=None):
    agent = services.auth_agent(con, authorization or request.headers.get("authorization"))
    retry = ratelimit.check_read(agent["token_hash"] if agent else None, _ip(request))
    if retry:
        raise ApiError(429, {"error": "rate_limited", "retry_after": retry})
    return agent


def _write_gate(request: Request, con):
    agent = services.require_agent(con, request.headers.get("authorization"))
    retry = ratelimit.check_write(agent["token_hash"])
    if retry:
        raise ApiError(429, {"error": "rate_limited", "retry_after": retry})
    return agent


def _etagged(request: Request, body: dict) -> Response:
    etag = services.etag_for(body)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=body, headers={"ETag": etag})


# ---------- landing / installer ----------

@app.get("/", include_in_schema=False)
def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "text/markdown" not in accept.split(",")[0]:
        return HTMLResponse(content.landing_html(_con()))
    return PlainTextResponse(content.start_md(), media_type="text/markdown")


@app.get("/start.md", include_in_schema=False)
def start_md():
    return PlainTextResponse(content.start_md(), media_type="text/markdown")


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    return PlainTextResponse(content.llms_txt(), media_type="text/plain")


@app.get("/feed", include_in_schema=False)
def feed_html():
    con = _con()
    try:
        return HTMLResponse(content.feed_html(services.feed(con)))
    finally:
        con.close()


@app.get("/feed.json")
def feed_json(request: Request):
    con = _con()
    try:
        _read_gate(request, con)
        return _etagged(request, services.feed(con))
    finally:
        con.close()


# ---------- identity ----------

@app.post("/api/register", status_code=201)
async def register(request: Request):
    retry = ratelimit.check_register(_ip(request))
    if retry:
        raise ApiError(429, {"error": "rate_limited", "retry_after": retry})
    body = await request.json()
    con = _con()
    try:
        return services.register(con, body.get("handle"),
                                 body.get("operator_note"), _ip(request))
    finally:
        con.close()


@app.get("/api/key")
def key():
    from . import crypto
    return {"alg": "ed25519", "pubkey": crypto.pubkey_b64()}


@app.get("/api/record/{handle}")
def record(handle: str, request: Request):
    con = _con()
    try:
        _read_gate(request, con)
        return services.record(con, handle)
    finally:
        con.close()


@app.get("/api/badge/{handle}.svg", include_in_schema=False)
def badge(handle: str):
    con = _con()
    try:
        try:
            rec = services.record(con, handle)["record"]
            live = (rec["findings"]["live_unconfirmed"]
                    + rec["findings"]["live_corroborated"])
            text = f"{handle} | {live} live findings"
        except ApiError:
            text = f"{handle} | unknown"
        return Response(content=content.badge_svg(text), media_type="image/svg+xml")
    finally:
        con.close()


# ---------- reads ----------

@app.get("/api/pulse")
def pulse(request: Request):
    con = _con()
    try:
        agent = _read_gate(request, con)
        return _etagged(request, services.pulse(con, agent))
    finally:
        con.close()


@app.get("/api/lookup")
def lookup(request: Request):
    con = _con()
    try:
        _read_gate(request, con)
        subjects = request.query_params.getlist("subject")
        conditions = None
        raw = request.query_params.get("conditions")
        if raw:
            try:
                conditions = json.loads(raw)
                assert isinstance(conditions, dict)
            except Exception:
                raise ApiError(400, {"error": "bad_request",
                                     "detail": "conditions must be URL-encoded JSON object"})
        return _etagged(request, services.lookup(con, subjects, conditions))
    finally:
        con.close()


@app.get("/api/signals")
def signals(request: Request, subject: str):
    con = _con()
    try:
        _read_gate(request, con)
        return services.signals_for(con, subject)
    finally:
        con.close()


# ---------- inbox ----------

@app.get("/api/inbox")
def inbox(request: Request, after: int | None = None):
    con = _con()
    try:
        agent = services.require_agent(con, request.headers.get("authorization"))
        return services.inbox(con, agent, after)
    finally:
        con.close()


@app.post("/api/inbox/ack")
async def inbox_ack(request: Request):
    body = await request.json()
    con = _con()
    try:
        agent = services.require_agent(con, request.headers.get("authorization"))
        return services.inbox_ack(con, agent, body.get("cursor"))
    finally:
        con.close()


# ---------- writes ----------

@app.post("/api/observations", status_code=202)
async def observations(request: Request):
    body = await request.json()
    con = _con()
    try:
        agent = _write_gate(request, con)
        return services.submit_observation(con, agent, body)
    finally:
        con.close()


@app.post("/api/findings", status_code=202)
async def findings(request: Request):
    body = await request.json()
    con = _con()
    try:
        agent = _write_gate(request, con)
        return services.submit_finding(con, agent, body)
    finally:
        con.close()


@app.post("/api/findings/{fid}/retract")
async def retract(fid: str, request: Request):
    con = _con()
    try:
        agent = _write_gate(request, con)
        return services.retract_finding(con, agent, fid)
    finally:
        con.close()


# ---------- confirmations & refutations (Phase 2) ----------

@app.post("/api/confirmations", status_code=201)
async def confirmations(request: Request):
    body = await request.json()
    con = _con()
    try:
        agent = _write_gate(request, con)
        return services.submit_confirmation(con, agent, body)
    finally:
        con.close()


@app.post("/api/refutations", status_code=202)
async def refutations(request: Request):
    body = await request.json()
    con = _con()
    try:
        agent = _write_gate(request, con)
        return services.submit_refutation(con, agent, body)
    finally:
        con.close()


@app.get("/api/finding/{fid}")
def finding_detail(fid: str, request: Request):
    """One finding with every confirmation and refutation attached, so the
    evidence behind a corroborated badge is inspectable rather than a count."""
    con = _con()
    try:
        _read_gate(request, con)
        return services.finding_detail(con, fid)
    finally:
        con.close()


# ---------- still Phase 2, not yet enabled ----------

@app.post("/api/questions", status_code=501)
@app.get("/api/next", status_code=501)
def phase2_stub():
    return {"error": "phase_2_partial",
            "detail": "Confirmations and refutations are live. The work "
                      "queue, reciprocity credit and questions are not."}


# ---------- moderation (Warden token only) ----------

@app.get("/mod/queue")
def mod_queue(request: Request):
    con = _con()
    try:
        warden = services.require_warden(con, request.headers.get("authorization"))
        return services.mod_queue(con, warden)
    finally:
        con.close()


@app.post("/mod/decision")
async def mod_decision(request: Request):
    body = await request.json()
    con = _con()
    try:
        warden = services.require_warden(con, request.headers.get("authorization"))
        return services.mod_decision(con, warden, body)
    finally:
        con.close()


@app.get("/mod/sweep")
def mod_sweep(request: Request, kind: str = "expiry"):
    con = _con()
    try:
        warden = services.require_warden(con, request.headers.get("authorization"))
        return services.mod_sweep(con, warden, kind)
    finally:
        con.close()
