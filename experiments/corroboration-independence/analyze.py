"""Analysis for the corroboration-independence experiment.

PRIMARY (pre-registered): among planted-FALSE claims, the rate at which a
claim clears a 2-of-3 "reproduced" bar, same-model arm vs cross-model arm.
Kill condition: the two arms statistically indistinguishable.

SECONDARY: among cells that all answered "reproduced", did they check the
same thing? Measured by overlap of the free-text `checked` field.
"""
import json, sys, math, re
from collections import defaultdict
from itertools import combinations

rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
errs = [r for r in rows if r["error"]]
rows = [r for r in rows if not r["error"]]

# ---------- assemble per (claim, arm) cells ----------
cell = defaultdict(list)
for r in rows:
    cell[(r["claim_id"], r["arm"])].append(r)

truth = {r["claim_id"]: r["truth"] for r in rows}
subject = {r["claim_id"]: r["subject"] for r in rows}

def fisher_exact(a, b, c, d):
    """Two-tailed Fisher exact on [[a,b],[c,d]]."""
    def lc(n, k):
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    n = a + b + c + d
    r1, r2, c1 = a + b, c + d, a + c
    def p(x):
        return math.exp(lc(r1, x) + lc(r2, c1 - x) - lc(n, c1))
    p0 = p(a)
    lo, hi = max(0, c1 - r2), min(r1, c1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 * (1 + 1e-9)))

def wilson(k, n):
    if n == 0:
        return (0.0, 0.0)
    z, ph = 1.96, k / n
    d = 1 + z*z/n
    c = (ph + z*z/(2*n)) / d
    h = z * math.sqrt(ph*(1-ph)/n + z*z/(4*n*n)) / d
    return (max(0, c - h), min(1, c + h))

print("=" * 74)
print("CORROBORATION INDEPENDENCE - RESULTS")
print("=" * 74)
complete = [k for k, v in cell.items() if len(v) == 3]
print(f"cells: {len(cell)} ({len(complete)} with all 3 evaluations) | "
      f"evaluations: {len(rows)} | errors: {len(errs)}")

# ---------- PRIMARY ----------
print("\n" + "-" * 74)
print("PRIMARY: planted-false claims clearing a 2-of-3 'reproduced' bar")
print("-" * 74)

res = {}
for arm in ("same", "cross"):
    for want_truth in (False, True):
        ids = [cid for cid in truth if truth[cid] == want_truth
               and len(cell.get((cid, arm), [])) == 3]
        cleared = [cid for cid in ids
                   if sum(1 for r in cell[(cid, arm)] if r["verdict"] == "reproduced") >= 2]
        res[(arm, want_truth)] = (len(cleared), len(ids), cleared)

for want_truth, label in ((False, "PLANTED FALSE (false corroboration)"),
                          (True, "TRUE (correct corroboration)")):
    print(f"\n{label}:")
    for arm in ("same", "cross"):
        k, n, _ = res[(arm, want_truth)]
        lo, hi = wilson(k, n)
        bar = "#" * int(round((k / n if n else 0) * 40))
        print(f"  {arm:5s}-model  {k:2d}/{n:2d} = {k/n*100 if n else 0:5.1f}%  "
              f"[95% CI {lo*100:4.1f}-{hi*100:4.1f}]  {bar}")
    ks, ns, _ = res[("same", want_truth)]
    kc, nc, _ = res[("cross", want_truth)]
    p = fisher_exact(ks, ns - ks, kc, nc - kc)
    print(f"  difference: {(ks/ns - kc/nc)*100 if ns and nc else 0:+.1f} pp   "
          f"Fisher exact two-tailed p = {p:.4f}"
          f"   -> {'SIGNIFICANT at 0.05' if p < 0.05 else 'not significant'}")
    if want_truth is False:
        primary_p = p

print(f"\n  PRE-REGISTERED VERDICT: ", end="")
ks, ns, _ = res[("same", False)]
kc, nc, _ = res[("cross", False)]
if primary_p < 0.05 and ks/ns > kc/nc:
    print("prediction SUPPORTED - same-model arm falsely corroborates more.")
elif primary_p < 0.05:
    print("prediction CONTRADICTED - cross-model arm falsely corroborates MORE.")
else:
    print("KILL CONDITION MET - arms indistinguishable; the prediction fails.")

# ---------- per-model error rates ----------
print("\n" + "-" * 74)
print("Per-model accuracy (does one model carry the effect?)")
print("-" * 74)
per = defaultdict(lambda: [0, 0, 0])   # model -> [correct, total, false-repro]
for r in rows:
    correct = ((r["verdict"] == "reproduced") == bool(r["truth"]))
    per[r["model"]][0] += correct
    per[r["model"]][1] += 1
    if not r["truth"] and r["verdict"] == "reproduced":
        per[r["model"]][2] += 1
for m, (c, t, fp) in sorted(per.items()):
    nf = sum(1 for r in rows if r["model"] == m and not r["truth"])
    print(f"  {m:8s} accuracy {c}/{t} = {c/t*100:5.1f}%   "
          f"affirmed a planted falsehood {fp}/{nf} = {fp/nf*100 if nf else 0:5.1f}%")

# ---------- SECONDARY: did 'reproduced' agreers check the same thing? ----------
print("\n" + "-" * 74)
print("SECONDARY: among unanimous 'reproduced' cells, did they check alike?")
print("-" * 74)
STOP = set("""the a an of to in on for and or is are was were be been being it its
this that with as at by from not no such which what when their there here them
you your i we they he she his her our us have has had do does did if then than
so but into over under about after before between during without within""".split())

def toks(s):
    return {w for w in re.findall(r"[a-z0-9_.]+", (s or "").lower())
            if w not in STOP and len(w) > 2}

def jac(a, b):
    u = a | b
    return len(a & b) / len(u) if u else 0.0

sims = defaultdict(list)
for (cid, arm), rs in cell.items():
    if len(rs) != 3 or any(r["verdict"] != "reproduced" for r in rs):
        continue
    ts = [toks(r["checked"]) for r in rs]
    pair = [jac(x, y) for x, y in combinations(ts, 2)]
    sims[(arm, bool(truth[cid]))].append(sum(pair) / len(pair))

for want_truth, label in ((False, "planted-false"), (True, "true")):
    print(f"\n  unanimous-reproduced cells on {label} claims:")
    for arm in ("same", "cross"):
        v = sims.get((arm, want_truth), [])
        if v:
            print(f"    {arm:5s}-model  n={len(v):2d}  mean overlap of "
                  f"'what I checked' = {sum(v)/len(v):.3f}")
        else:
            print(f"    {arm:5s}-model  n= 0")

print("\n" + "=" * 74)
