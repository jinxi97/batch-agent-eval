# batch-agent-eval

Run [Harbor](https://pypi.org/project/harbor/) agent evals (e.g. SWE-bench Verified) with:

| Component | Choice |
|---|---|
| Harness | Harbor `0.26.0` (pinned in `pyproject.toml` / `uv.lock`) |
| Agent | `terminus-2` |
| Model | `moonshotai/Kimi-K3.1-Pro` on a **Modal Shared Endpoint** (OpenAI-compatible) |
| Task containers | **Modal sandboxes** (`-e modal`) — SWE-bench images are x86_64, so this avoids emulation on Apple Silicon |
| Dataset | `swe-bench/swe-bench-verified` (500 tasks) from the Harbor registry |

```
.
├── configs/kimi-k3.1-pro-modal.yaml   # Harbor JobConfig: agent, model, kwargs, pricing, environment
├── scripts/run-eval.sh                # wrapper: loads .env, injects endpoint URL, calls `harbor run`
├── .env.example                       # MODAL_PROXY_KEY, MODAL_ENDPOINT_URL
└── jobs/                              # results (only summary files are committed; see §3)
```

## 1. Initialize (one time)

Prerequisites: [uv](https://docs.astral.sh/uv/) and a Modal account.

```bash
# Install pinned Harbor + Modal CLI into .venv
uv sync

# Authenticate Modal (used to run the task sandboxes)
uv run modal token new
```

### Create the model endpoint

Shared Endpoints require a **proxy token** (`wk-…` / `ws-…`), which is separate from the
`ak-…` API token created above.

```bash
# 1. Create a proxy token — the secret is shown only once, save it
uv run modal workspace proxy-tokens create --name harbor-eval

# 2. If your workspace uses RBAC, allow the token in your environment
uv run modal environment list
uv run modal workspace proxy-tokens allow wk-XXXX <env-name>

# 3. Create the Shared Endpoint and get its URL
uv run modal endpoint create --shared --model moonshotai/Kimi-K3.1-Pro
uv run modal endpoint info <endpoint-id>
```

### Configure credentials

```bash
cp .env.example .env
# edit .env:
#   MODAL_PROXY_KEY=wk-<id>.ws-<secret>      (token ID and secret joined with a ".")
#   MODAL_ENDPOINT_URL=https://<...>.modal.direct
```

Check the endpoint and model ID (should list `moonshotai/Kimi-K3.1-Pro`):

```bash
set -a; source .env; set +a
curl "$MODAL_ENDPOINT_URL/v1/models" -H "Authorization: Bearer $MODAL_PROXY_KEY"
```

## 2. Run the eval

### Sanity check (no model)

Runs the task's reference solution to verify the image, Modal sandbox and grader. Expect reward `1.0`.

```bash
uv run harbor run -t swe-bench/django__django-11099 -a oracle -e modal
```

### Run Kimi-K3.1-Pro

```bash
# Default: single task swe-bench/django__django-11099
scripts/run-eval.sh --job-name my-django-run

# A different single task (IDs need the "swe-bench/" prefix)
scripts/run-eval.sh -t swe-bench/astropy__astropy-12907

# A slice of the dataset: 20 tasks, 8 in parallel
scripts/run-eval.sh -d swe-bench/swe-bench-verified -l 20 -n 8

# Filter by glob
scripts/run-eval.sh -d swe-bench/swe-bench-verified -i 'swe-bench/django__*'

# Full SWE-bench Verified (500 tasks)
scripts/run-eval.sh -d swe-bench/swe-bench-verified -n 16
```

Any extra arguments go straight to `harbor run` (see `uv run harbor run --help`). Useful ones:
`--dry-run` (validate without running), `--print-config` (show resolved config),
`--ak reasoning_effort=high` (endpoint accepts `low|high|max`; omitted = endpoint default),
`-k 3` (attempts per task), `-r 2` (retries on infra errors).

## 3. Results

Each run writes to `jobs/<job-name>/`:

| File | Contents |
|---|---|
| `result.json` | Job summary: mean reward, trial counts, total tokens, `cost_usd` |
| `lock.json` | Exact Harbor version, task digests and agent config used (for reproducibility) |
| `<trial>/result.json` | Per-trial reward, token usage, timings, errors |
| `<trial>/verifier/reward.txt` | `1` = SWE-bench tests pass, `0` = fail |
| `<trial>/verifier/test-stdout.txt` | Grader test output |
| `<trial>/agent/trajectory.json` | Every agent turn with per-turn token metrics |
| `<trial>/agent/recording.cast` | Terminal recording (`asciinema play …`) |

### What gets committed

`.gitignore` tracks only the small summary files, a few KB per task: the job's `result.json`,
`config.json` and `lock.json`, and each trial's `result.json`, `verifier/reward.txt` and
`verifier/report.json`. Together they record the scores, token counts, cost, and exact versions and
config used. Trajectories, terminal recordings and logs stay local. Run `git add jobs/` after a
run to stage that run's summaries.

Note: this repo is public, and the summaries include the endpoint URL (which contains the Modal
workspace name) and the local path the job ran from. Neither is a credential; the proxy key is
never written to `jobs/`.

Browse in a web UI:

```bash
uv run harbor view jobs
```

## Cost accounting

Modal bills Kimi-K3.1-Pro per token (no cache-write charge):

| Token type | USD / 1M |
|---|---|
| Prompt (uncached) | $2.00 |
| Cached prompt | $0.20 |
| Completion | $10.00 |

These prices are set in the config's `model_info`, so Harbor computes `cost_usd` itself. The endpoint
reports `prompt_tokens_details.cached_tokens`, which Harbor records as `n_cache_tokens`
(`n_input_tokens` *includes* cached tokens):

```
cost = (n_input_tokens − n_cache_tokens) × 2e-6 + n_cache_tokens × 2e-7 + n_output_tokens × 1e-5
```

Reference run (`jobs/kimi-k31-pro-django-11099-script/`, `swe-bench/django__django-11099`,
2026-11-17, run with `scripts/run-eval.sh` and this config): reward 1.0, 5 turns, 1m 21s trial time,
14,865 input / 11,328 cached / 2,022 output tokens → `cost_usd` **$0.0296**, which equals the formula
above. An earlier run of the same task (not committed) used 12 turns and cost $0.090, and Modal's
bill for it matched Harbor's token counts exactly. Cost varies between runs of the same task with
the number of turns the model takes.

## Config notes

- **`interleaved_thinking: true`** — the endpoint returns reasoning in `reasoning_content`; this feeds
  it back on later turns, as Kimi expects in agent loops.
- **`session_id_headers: [Modal-Routing-Affinity-Key]`** — sends a per-trial ID in Modal's routing
  header so all turns of a trial hit the same container and reuse its prompt-prefix KV cache
  (76% of input tokens were cache hits in the reference run). It doesn't configure the cache, only routing; it affects
  speed/cost, not results. (Harbor's docstring mentions `Modal-Session-ID`; that is for Modal
  *Servers* — Endpoints use `Modal-Routing-Affinity-Key`.)
- **`model_info`** — LiteLLM doesn't know this model, so the 1M-token context window (used for
  terminus-2's context summarization) and pricing are declared here.
- **Reasoning effort** — not set, so the endpoint default applies. Don't pass `--effort default`:
  Harbor sends the literal string `default`, which the endpoint doesn't accept.
