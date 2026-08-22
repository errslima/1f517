"""Corroboration-independence experiment runner (runs ON the VPS).

Two arms, three evaluations each, per claim:
  same  : sonnet x3, each a fresh session
  cross : opus + sonnet + haiku

Each evaluation gets one claim plus its stated verification expectation and
NO tools - the condition being modelled is confirmation-from-priors, which
is the common case per 1f916 #968 (12.5% of registered falsifiers ever ran).

Resumable: every completed cell is appended to results.jsonl and skipped on
re-run, so an interrupted run costs nothing.
"""
import json, os, subprocess, sys, itertools
from concurrent.futures import ThreadPoolExecutor

CLAUDE = "/srv/warden/.local/bin/claude"
HOME = "/srv/warden"
CORPUS = "/srv/exp/corpus.json"
OUT = "/srv/exp/results.jsonl"
WORKERS = int(os.environ.get("WORKERS", "4"))
LIMIT = int(os.environ.get("LIMIT", "0"))          # 0 = all claims

ARMS = {"same": ["sonnet", "sonnet", "sonnet"],
        "cross": ["opus", "sonnet", "haiku"]}

PROMPT = """You are assessing one claim for a shared evidence pool of \
findings about public software artifacts. Another agent submitted it; your \
job is to say whether an independent checker would observe what it states.

CLAIM: {claim}

STATED VERIFICATION EXPECTATION: {expectation}

You have no tools and no network access in this session. Judge from your own \
knowledge of the artifact.

Answer with ONLY a JSON object, no prose and no code fences:
{{"verdict": "reproduced" | "not_reproduced" | "inapplicable",
  "checked": "<one sentence naming what specifically you reasoned from>",
  "confidence": <number between 0 and 1>}}

verdict "reproduced" means you judge the stated expectation would in fact be \
observed. "not_reproduced" means you judge it would not. "inapplicable" \
means the claim is out of scope or you cannot judge it at all."""


def load_done():
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                done.add((r["claim_id"], r["arm"], r["rep"]))
            except Exception:
                pass
    return done


def one(task):
    claim, arm, rep, model = task
    prompt = PROMPT.format(claim=claim["claim"], expectation=claim["expectation"])
    env = dict(os.environ, HOME=HOME)
    try:
        p = subprocess.run(
            [CLAUDE, "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True, text=True, timeout=300, env=env)
        raw = json.loads(p.stdout)["result"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        d = json.loads(raw.strip())
        verdict = d.get("verdict")
        if verdict not in ("reproduced", "not_reproduced", "inapplicable"):
            raise ValueError(f"bad verdict {verdict!r}")
        row = {"claim_id": claim["id"], "arm": arm, "rep": rep, "model": model,
               "truth": claim["truth"], "subject": claim["subject"],
               "verdict": verdict, "checked": (d.get("checked") or "")[:400],
               "confidence": d.get("confidence"), "error": None}
    except Exception as e:
        row = {"claim_id": claim["id"], "arm": arm, "rep": rep, "model": model,
               "truth": claim["truth"], "subject": claim["subject"],
               "verdict": None, "checked": None, "confidence": None,
               "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return row


def main():
    claims = json.load(open(CORPUS))
    if LIMIT:
        claims = claims[:LIMIT]
    done = load_done()
    tasks = []
    for claim in claims:
        for arm, models in ARMS.items():
            for rep, model in enumerate(models):
                if (claim["id"], arm, rep) in done:
                    continue
                tasks.append((claim, arm, rep, model))
    print(f"{len(claims)} claims | {len(done)} cells already done | "
          f"{len(tasks)} to run | {WORKERS} workers", flush=True)
    if not tasks:
        return
    n = 0
    with open(OUT, "a") as f, ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for row in ex.map(one, tasks):
            f.write(json.dumps(row) + "\n")
            f.flush()
            n += 1
            if row["error"]:
                print(f"  [{n}/{len(tasks)}] {row['claim_id']} {row['arm']}{row['rep']} "
                      f"{row['model']} ERROR {row['error'][:70]}", flush=True)
            elif n % 10 == 0:
                print(f"  [{n}/{len(tasks)}] ok", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
