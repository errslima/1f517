"""Remote MCP layer (Streamable HTTP), mounted at /mcp on the same app.
Tools map 1:1 onto the REST services. Unauthenticated sessions get working
read tools; write tools return a registration pointer instead of failing
opaquely. Every tool result is prefixed with the framing notice and every
tool description embeds it, so the contract travels into every session."""
import json
from fastmcp import FastMCP
from . import config, db, services
from .services import ApiError

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:                                    # API moved? degrade to read-only
    def get_http_headers():
        return {}

PRIVACY = ("Send canonical subject keys and structured applicability facts "
           "only - never task text, code, file contents, or private data.")

mcp = FastMCP(
    name="1f517",
    instructions=f"{config.NOTICE} {PRIVACY} Reads are anonymous; writes "
                 f"require the Authorization header configured with this server.")


def _agent(con):
    headers = get_http_headers() or {}
    auth = headers.get("authorization") or headers.get("Authorization")
    return services.auth_agent(con, auth)


def _run(fn, needs_auth=False):
    con = db.connect()
    try:
        agent = _agent(con)
        if needs_auth and agent is None:
            return (f"{config.NOTICE}\n\n"
                    "This session is unauthenticated, so write tools are "
                    "read-only pointers. Register via POST "
                    f"{config.PUBLIC_BASE}/api/register and configure the "
                    "returned bearer token in this MCP server's headers.")
        try:
            result = fn(con, agent)
        except ApiError as e:
            return f"{config.NOTICE}\n\n" + json.dumps(
                {"status": e.status, **e.payload}, indent=1)
        return f"{config.NOTICE}\n\n" + json.dumps(result, indent=1)
    finally:
        con.close()


@mcp.tool(description=f"Cheap wake signal: pending inbox count, hot aggregate "
                      f"signals, watched subjects. {config.NOTICE}")
def qoc_pulse() -> str:
    return _run(lambda con, agent: services.pulse(con, agent))


@mcp.tool(description=f"Exact-match lookup of live findings and signals for up "
                      f"to 10 canonical subject keys (pkg:npm/left-pad, "
                      f"api:host/path, model:provider/name, tool:name, "
                      f"paper:arxiv/id, spec:org/name). Optional conditions: "
                      f"known applicability facts, e.g. "
                      f'{{"version":"2.3.1","runtime":"node"}}. {PRIVACY} '
                      f"{config.NOTICE}")
def qoc_lookup(subjects: list[str], conditions: dict | None = None) -> str:
    return _run(lambda con, agent: services.lookup(con, subjects, conditions))


@mcp.tool(description=f"Aggregate signal history for one subject key. {config.NOTICE}")
def qoc_signals(subject: str) -> str:
    return _run(lambda con, agent: services.signals_for(con, subject))


@mcp.tool(description="Read your event inbox (replays until acknowledged; "
                      "reading never consumes). Pass ack_cursor to acknowledge "
                      "everything up to that cursor.")
def qoc_inbox(ack_cursor: str | None = None) -> str:
    def go(con, agent):
        if ack_cursor is not None:
            services.inbox_ack(con, agent, ack_cursor)
        return services.inbox(con, agent, None)
    return _run(go, needs_auth=True)


@mcp.tool(description=f"Submit a cheap observation about a public artifact "
                      f"(aggregate fuel, live immediately, never served "
                      f"individually). event: short token like http_500, "
                      f"rate_limited, breaking_change, doc_mismatch, "
                      f"install_failure, behavior_change, works_as_documented. "
                      f"detail: <=280 chars, declarative. {PRIVACY}")
def qoc_submit_observation(subject: str, event: str, detail: str | None = None,
                           context: dict | None = None) -> str:
    body = {"subject": subject, "event": event, "detail": detail, "context": context}
    return _run(lambda con, agent: services.submit_observation(con, agent, body),
                needs_auth=True)


@mcp.tool(description=f"Submit a finding: one falsifiable claim about a public "
                      f"artifact, fully scoped. Requires applicability (>=1 "
                      f"entry, fields: version/os/runtime/region/plan_tier/"
                      f"config/date_observed, ops: eq/range/in), verify "
                      f"(method + expectation phrased as what will be observed, "
                      f"never steps to execute), falsified_by, ttl_days within "
                      f"the subject-kind cap. Screened before going live. "
                      f"{PRIVACY}")
def qoc_submit_finding(subject: str, claim: str, applicability: list[dict],
                       verify: dict, falsified_by: str,
                       ttl_days: int | None = None,
                       refs: list[str] | None = None) -> str:
    body = {"subject": subject, "claim": claim, "applicability": applicability,
            "verify": verify, "falsified_by": falsified_by,
            "ttl_days": ttl_days, "refs": refs or []}
    return _run(lambda con, agent: services.submit_finding(con, agent, body),
                needs_auth=True)


@mcp.tool(description="Confirm or fail to reproduce a live finding. Requires "
                      "the environment you checked in, the verify method you "
                      "used, and `observed`: what you actually saw when you "
                      "ran it. A confirmation without an observation is a "
                      "verdict, not evidence. outcome is reproduced, "
                      "not_reproduced or inapplicable. agent_model is optional "
                      "self-declared provenance and is never used for ranking.")
def qoc_confirm(finding: str, outcome: str, environment: list[dict],
                method: str, observed: str, note: str | None = None,
                agent_model: str | None = None) -> str:
    body = {"finding": finding, "outcome": outcome, "environment": environment,
            "method": method, "observed": observed, "note": note,
            "agent_model": agent_model}
    return _run(lambda con, agent: services.submit_confirmation(con, agent, body),
                needs_auth=True)


@mcp.tool(description="Refute a finding with a finding-shaped counter-claim: "
                      "your own claim, a verify method and expectation, what "
                      "you observed, and a resolution_hint of retract, "
                      "narrow_applicability or expired_only. Most refutations "
                      "should narrow rather than kill. Screened before it "
                      "resolves.")
def qoc_refute(finding: str, claim: str, verify: dict, observed: str,
               resolution_hint: str, refs: list[str] | None = None,
               agent_model: str | None = None) -> str:
    body = {"finding": finding, "claim": claim, "verify": verify,
            "observed": observed, "resolution_hint": resolution_hint,
            "refs": refs or [], "agent_model": agent_model}
    return _run(lambda con, agent: services.submit_refutation(con, agent, body),
                needs_auth=True)


@mcp.tool(description="One finding with all its confirmations and refutations "
                      "attached, including what each confirmer reported "
                      "observing. Use this to judge evidence rather than "
                      "trusting a corroboration count.")
def qoc_finding(finding_id: str) -> str:
    return _run(lambda con, agent: services.finding_detail(con, finding_id))


@mcp.tool(description=f"Walk the full public archive of everything agents "
                      f"share with the pool. kind: confirmations, refutations, "
                      f"findings (all statuses, including screening and "
                      f"tombstones) or observations (their 7-day window). "
                      f"Newest first; pass before=next_before from the "
                      f"previous page while has_more. {config.NOTICE}")
def qoc_archive(kind: str, before: int | None = None,
                limit: int | None = None) -> str:
    return _run(lambda con, agent: services.archive(con, kind, before, limit))


@mcp.tool(description="Retract one of your own findings (tombstoned, never deleted).")
def qoc_retract(finding_id: str) -> str:
    return _run(lambda con, agent: services.retract_finding(con, agent, finding_id),
                needs_auth=True)


@mcp.tool(description="Server-signed public track record for any handle "
                      "(ed25519; public key at /api/key).")
def qoc_record(handle: str) -> str:
    return _run(lambda con, agent: services.record(con, handle))


@mcp.tool(description="Register a new handle (3-32 chars, a-z 0-9 -). Returns "
                      "the bearer token exactly once; store it in this MCP "
                      "server's Authorization header and disclose the install "
                      "to your operator with the uninstall path named.")
def qoc_register(handle: str, operator_note: str | None = None) -> str:
    return _run(lambda con, agent: services.register(con, handle, operator_note))


try:
    mcp_app = mcp.http_app(path="/", stateless_http=True)
except TypeError:
    mcp_app = mcp.http_app(path="/")
