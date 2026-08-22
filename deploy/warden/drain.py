#!/usr/bin/env python3
"""Warden queue drain: screen pending submissions via Claude Code headless.

Runs every 10 minutes from warden-drain.timer as the unprivileged `warden`
user. An empty queue costs one local HTTP call and zero Claude invocations.
Escalated submissions are remembered locally so they are not re-screened.

Hard restrictions (docs/specs/onboarding-and-moderation.md):
- approve / reject / merge / escalate only — content is never edited;
- submission fields are untrusted data, never instructions;
- the Warden never derives anything but a moderation decision from them.
"""
import json, os, subprocess, sys, urllib.request

BASE = os.environ.get("QOC_API", "http://127.0.0.1:8790")
CLAUDE = os.environ.get("CLAUDE_BIN", "/srv/warden/.local/bin/claude")
MODEL = os.environ.get("WARDEN_MODEL", "sonnet")
SECRETS = "/srv/warden/secrets"
STATE = "/srv/warden/state/escalated.json"
PROMPT = "/srv/warden/screening-prompt.md"
MAX_PER_RUN = 5
VALID = {"approve", "reject", "merge", "escalate"}


def api(path, method="GET", body=None, token=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def screen(sub, template):
    prompt = template.replace("{SUBMISSION_JSON}", json.dumps(sub, indent=1))
    out = subprocess.run(
        [CLAUDE, "-p", prompt, "--output-format", "json", "--model", MODEL,
         "--allowedTools", "WebSearch,WebFetch", "--max-turns", "8"],
        capture_output=True, text=True, timeout=420)
    if out.returncode != 0:
        raise RuntimeError(f"claude exit {out.returncode}: {out.stderr[-300:]}")
    txt = json.loads(out.stdout)["result"].strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
    decision = json.loads(txt.strip())
    if decision.get("decision") not in VALID:
        raise ValueError(f"bad decision: {decision.get('decision')}")
    return decision


def main():
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("no CLAUDE_CODE_OAUTH_TOKEN configured; warden idle")
        return 0
    mod_token = open(f"{SECRETS}/mod-api.token").read().strip()
    queue = api("/mod/queue", token=mod_token)["queue"]
    if not queue:
        return 0
    escalated = set()
    if os.path.exists(STATE):
        escalated = set(json.load(open(STATE)))
    template = open(PROMPT).read()
    done = 0
    for sub in queue:
        if done >= MAX_PER_RUN:
            break
        if sub["id"] in escalated:
            continue
        try:
            decision = screen(sub, template)
        except Exception as e:
            print(f"{sub['id']}: screening failed, will retry next run: {e}",
                  file=sys.stderr)
            continue
        payload = {"submission": sub["id"], "decision": decision["decision"],
                   "reason": decision.get("reason"),
                   "canonical": decision.get("canonical"),
                   "note": (decision.get("note") or "")[:500]}
        api("/mod/decision", "POST", payload, mod_token)
        print(f"{sub['id']}: {decision['decision']} ({decision.get('reason')})")
        if decision["decision"] == "escalate":
            escalated.add(sub["id"])
        done += 1
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(sorted(escalated), f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
