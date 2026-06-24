# DFOptimizer

DFOptimizer turns [DFDiagnoser](https://github.com/LLNL/dfdiagnoser)'s **diagnosis
findings** into **ActionPlans** — concrete knob changes (e.g. *raise dataloader
prefetch from 2 to 4*) — and, online, feeds them back to the running application to
**re-tune it in flight**. It runs the same way **offline** (replay saved findings to
inspect the decisions) and **online** (consume a live stream over Mofka or ZMQ and
actuate).

## What it does

A finding from DFDiagnoser says *what's wrong, how persistent, and what class of fix*
(`fetch_pressure`, `persistent_pressure`, `opportunity_tags=[dataloader_prefetch,
reader_parallelism]`, …). DFOptimizer's **planner** maps those `opportunity_tags` to
registered **knobs** via each knob's `responds_to` rule and emits an `ActionPlan`,
gated so it acts responsibly:

- **persistence / trend / severity** — only act on a bottleneck that's real and sticky
- **cooldown + pending-plan** — one change per knob at a time; don't thrash
- **apply_when** — apply at a safe boundary (e.g. `window_boundary`)

Knobs come from the application: it registers them (id, range, `responds_to`,
`target_function`) through `OptimizerContext`, which also **receives the plans and
applies them** at the next boundary — closing the diagnose → tune → re-measure loop.
For local/offline use, the bundled DLIO knobs can be bootstrapped with
`DFOPTIMIZER_BOOTSTRAP_DLIO=1`.

## Installation

```bash
pip install dfoptimizer                 # core (offline replay)
pip install "dfoptimizer[streaming]"    # + online transports (pyzmq / mofka)
```

From source: `uv sync && uv pip install -e .`

## Usage

`main.py` selects a transport with `--transport {mofka,zmq,file}`.

### Offline — replay saved findings into plans

Inspect what the planner *would* do for a recorded set of findings (a `.jsonl` of
DFDiagnoser finding wire-dicts, or a JSON array) — no transport, no actuation:

```bash
DFOPTIMIZER_BOOTSTRAP_DLIO=1 python main.py --transport file --findings-file findings.jsonl
```

```text
# (structured JSON logs, condensed)
optimizer.plan   knob_id=dlio.prefetch_size  old_value=2  new_value=3
                 rationale="fetch_pressure: persistent_pressure (persistence=5) -> dataloader_prefetch"
optimizer.plan   knob_id=dlio.read_threads   old_value=1  new_value=8
                 rationale="fetch_pressure: persistent_pressure (persistence=5) -> reader_parallelism"
optimizer.file.done   findings=1  plan_count=2  knob_state={'dlio.prefetch_size': 3, 'dlio.read_threads': 8}
```

(A persistent `fetch_pressure` finding → raise `prefetch_size` and `read_threads`,
the fixes its `opportunity_tags` map to.)

### Online — live actuation (ZMQ)

Consume findings DFDiagnoser streams (`diagnose_zmq ... input.output_address=...`) and
publish the plans onward; the app's `OptimizerContext` (zmq) applies them:

```bash
python main.py --transport zmq \
    --address "tcp://127.0.0.1:5557" \
    --plans-address "tcp://*:5558"
```

### Online — live actuation (Mofka, LiveFlow)

The full cluster loop — consume `diagnosis_findings`, publish `optimizer_plans`,
learn knobs from the app's registry:

```bash
python main.py \
    --group-file "$MOFKA_GROUP_FILE" \
    --input-topic diagnosis_findings \
    --output-topic optimizer_plans \
    --registry-topic optimizer_registry
```

All three paths run the same planner; only the finding source / plan sink differ.

## Inputs and outputs

- **Input:** DFDiagnoser findings (`finding_type`, `scope`, `motif`, `severity_score`,
  `prevalence`, `persistence`, `trend_direction`, `opportunity_tags`,
  `recommendation_bundle`, `key_metrics`) — a `.jsonl`/array file (offline) or a
  Mofka/ZMQ stream (online).
- **Output:** `ActionPlan`s (`knob_id`, `old_value` → `new_value`, `apply_when`,
  `target_function`, `rationale`) — returned/logged (offline) or published to the app
  for in-flight application (online).

## Requirements

- Python >= 3.9
- DFDiagnoser findings as input
- Online transports: `pyzmq` (ZMQ) or `mochi-mofka` (Mofka), via the `[streaming]` extra

## License

MIT — see [LICENSE](LICENSE).
