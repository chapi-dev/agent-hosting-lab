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
| State test: naive FAIL, hardened/hosted/hybrid PASS | `01_session_state_20260826T040450Z.json` | Last run, against the final deployed images (`v6` + redeployed hosted agent) |
| Cold start: hosted +7818 ms, hybrid +7592 ms | `02_cold_start_20260826T030121Z.json` | Largest sample (4 rounds) of the three cold-start runs |
| Deployment surface: 122 / 509 / 53, 9.6x, router 141 | `03_deployment_surface_20260826T040725Z.json` | Static analysis of the final source tree |
| Pre-warm: hosted 69.3%, hybrid 70.7%, controls ±10% | `04_prewarm_20260826T042647Z.json` | 8 rounds — the smallest n at which the self-hosted controls settle |
| Session pre-create: body 100% / 3174 ms, header 0% / 9458 ms | `05_session_precreate_20260826T032820Z.json` | Largest sample (4 rounds); reproduced at 3 rounds in the `T041352Z` run |

## Reproducibility

The two headline effects were measured more than once, on different deployments, hours apart.

**Cold start penalty** (`02_cold_start_*`), median first turn minus median warm turn:

| Run | naive | hardened | hosted | hybrid |
|---|---|---|---|---|
| `T025712Z` (3 rounds) | +174 ms | −289 ms | **+6840 ms** | −520 ms † |
| `T030121Z` (4 rounds) | −164 ms | +225 ms | **+7818 ms** | **+7592 ms** |
| `T040725Z` (3 rounds) | +380 ms | −379 ms | **+6801 ms** | **+6522 ms** |

† The `T025712Z` hybrid row predates a fix in the experiment harness: it did not propagate the
session handles back to the router, so every turn opened a fresh sandbox and paid a full cold
start — a flat ~9.6 s per turn instead of one cold start followed by warm turns. The bug is
documented in `experiments/02_cold_start.py` and in `docs/04-cold-start-and-scaling.md`,
because the same mistake in a real client produces the same flat latency in production. The run
is kept rather than deleted; deleting inconvenient runs is how measurement turns into marketing.

The self-hosted controls scatter around zero by ±380 ms. The hosted effect is roughly 20x that.
No reading of the data supports a different conclusion.

**Session attachment** (`05_session_precreate_*`), first turn against a pre-created session:

| Run | body: reused | body: first turn | header: reused | header: first turn |
|---|---|---|---|---|
| `T032820Z` (4 rounds) | 4/4 | 3174 ms | 0/4 | 9458 ms |
| `T041352Z` (3 rounds) | 3/3 | 3458 ms | 0/3 | 8776 ms |

Seven attempts per mechanism, across two runs. The body field reattached every time; the header
reattached never. This is the finding with the most direct impact on production latency, so it
is the one we were most careful to repeat.

## Regenerating

```powershell
./scripts/run-experiments.ps1              # all five, 4 rounds
./scripts/run-experiments.ps1 -Only 04 -Rounds 8
```

Results are timestamped, so re-running never overwrites the evidence behind the documents.
The endpoints referenced inside these files belong to a throwaway lab subscription and are
expected to be gone by the time you read this; the numbers are the artefact, not the URLs.
