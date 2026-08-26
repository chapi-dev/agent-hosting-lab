# Evidence index

Every number in `README.md` and `docs/` comes from one of the JSON files in this directory.
They are committed on purpose: a claim about hosting models is only worth as much as the run
behind it, and a reader should be able to check ours without redeploying anything.

Files are named `<experiment>_<UTC timestamp>.json`. Several experiments were run more than
once — sometimes to raise the sample size, sometimes after a fix. The table below says which
run each documented figure comes from, and why that one.

## Quoted runs

| Documented figure | Source file | Why this run |
|---|---|---|
| State test: naive FAIL, hardened/hosted/hybrid PASS | `01_session_state_20260826T051222Z.json` | Last run, against the final images (`v8`, signed session handles) |
| Cold start: hosted +7818 ms, hybrid +7592 ms | `02_cold_start_20260826T030121Z.json` | The run reproduced in the README table; confirmed by three further runs below |
| Deployment surface: 122 / 527 / 53, 9.94x, router 175 | `03_deployment_surface_20260826T052951Z.json` | Static analysis of the final source tree |
| Pre-warm: hosted 69.3%, hybrid 70.7%, controls ±10% | `04_prewarm_20260826T042647Z.json` | 8 rounds — the smallest n at which the self-hosted controls settle |
| Session pre-create: body 100% / 3174 ms, header 0% / 9458 ms | `05_session_precreate_20260826T032820Z.json` | Largest sample (4 rounds); reproduced at 3 rounds in the `T041352Z` run |
| Session isolation: id alone reads the sandbox; router returns 403 | `06_session_isolation_20260826T051040Z.json` | Single run — a categorical result, not a measurement (see below) |

### A note on experiment 6

It is the only experiment quoted from one run, because it does not measure a quantity. Each
probe has a discrete outcome that either happens or does not, and both directions are pinned by
a control in the same run:

- the victim session returns *"Your trip includes: Barcelona"* while a different id returns
  *"Your trip is empty"* — so the leak is not the model being agreeable;
- the owner's own handle returns 200 while another user's returns 403 — so the 403 is not the
  router being broken.

Repetition would add nothing that the controls do not already establish.

## Reproducibility

The two headline effects were measured more than once, on different deployments, hours apart.

**Cold start penalty** (`02_cold_start_*`), median first turn minus median warm turn:

| Run | naive | hardened | hosted | hybrid |
|---|---|---|---|---|
| `T025712Z` (3 rounds) | +174 ms | −289 ms | **+6840 ms** | −520 ms † |
| `T030121Z` (4 rounds) | −164 ms | +225 ms | **+7818 ms** | **+7592 ms** |
| `T040725Z` (3 rounds) | +380 ms | −379 ms | **+6801 ms** | **+6522 ms** |
| `T051657Z` (4 rounds) ‡ | −340 ms | −54 ms | **+8985 ms** | **+7365 ms** |

† The `T025712Z` hybrid row predates a fix in the experiment harness: it did not propagate the
session handles back to the router, so every turn opened a fresh sandbox and paid a full cold
start — a flat ~9.6 s per turn instead of one cold start followed by warm turns. The bug is
documented in `experiments/02_cold_start.py` and in `docs/04-cold-start-and-scaling.md`,
because the same mistake in a real client produces the same flat latency in production. The run
is kept rather than deleted; deleting inconvenient runs is how measurement turns into marketing.

‡ First run after the router moved from raw session ids to signed handles. It is included
because a security refactor that touches the session path is exactly the kind of change that
could have quietly broken reattachment — the numbers landing in the same range as three earlier
runs is the evidence that it did not.

The self-hosted controls scatter around zero by ±380 ms. The hosted effect is roughly 20x that.
No reading of the data supports a different conclusion.

**Session attachment** (`05_session_precreate_*`), first turn against a pre-created session:

| Run | body: reused | body: first turn | header: reused | header: first turn |
|---|---|---|---|---|
| `T032820Z` (4 rounds) | 4/4 | 3174 ms | 0/4 | 9458 ms |
| `T041352Z` (3 rounds) | 3/3 | 3458 ms | 0/3 | 8776 ms |
| `T054140Z` (4 rounds) | 4/4 | 3490 ms | 0/4 | 9338 ms |
| `T111158Z` (4 rounds) | 4/4 | 4710 ms | 0/4 | 10986 ms |

Fifteen attempts per mechanism, across four runs hours apart — the last one after the agent was
changed to read `FOUNDRY_PROJECT_ENDPOINT`. The body field reattached every time; the header
reattached never, and its first turn never dropped below 8.7 s. This is the finding with the
most direct impact on production latency, so it is the one we were most careful to repeat.

**Pre-warm** (`04_prewarm_*`) is quoted from the 8-round run for one reason: at 4 rounds the
self-hosted controls are still noisy (−68.1% and −10.8% in `T052311Z`), which is what a control
looks like when a single slow round moves the median. At 8 rounds they settle to −9.5% and
+10.0%. The hosted and hybrid savings barely move between the two (57.6%/50.6% at n=4,
69.3%/70.7% at n=8) — the effect was never in doubt, only the precision of the number. We quote
the run where the controls are trustworthy, not the one with the friendliest headline.

## Regenerating

```powershell
./scripts/run-experiments.ps1              # all six, 4 rounds
./scripts/run-experiments.ps1 -Only 04 -Rounds 8
```

> `-Only` takes zero-padded ids (`01`, not `1`). PowerShell parses bare `-Only 01,02` as the
> integers 1 and 2; the runner pads them back and rejects unknown ids, because the earlier
> version silently skipped every experiment and reported success.

Results are timestamped, so re-running never overwrites the evidence behind the documents.
The endpoints referenced inside these files belong to a throwaway lab subscription and are
expected to be gone by the time you read this; the numbers are the artefact, not the URLs.
