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
├── configs/swe-bench-verified-60.yaml # fixed 60-task subset (generated; see "60-task subset" below)
├── scripts/run-eval.sh                # wrapper: loads .env, injects endpoint URL, calls `harbor run`
├── scripts/sample_tasks.py            # generates task-subset configs (stratified, seeded)
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

# The fixed 60-task subset (see below)
scripts/run-eval.sh -c configs/swe-bench-verified-60.yaml --job-name kimi-k31-pro-swv60

# Filter by glob
scripts/run-eval.sh -d swe-bench/swe-bench-verified -i 'swe-bench/django__*'

# Full SWE-bench Verified (500 tasks)
scripts/run-eval.sh -d swe-bench/swe-bench-verified -n 16
```

### 60-task subset

`configs/swe-bench-verified-60.yaml` lists 60 SWE-bench Verified tasks, so every run of the subset
uses exactly the same tasks.

- **Which tasks are eligible:** only tasks labelled `<15 min fix` or `15 min - 1 hour` (455 of the
  500). The 45 tasks labelled `1-4 hours` or `>4 hours` are left out. These labels are SWE-bench's
  estimate of how long a human engineer would take to fix the issue, not how long the agent runs.
- **How the 60 were picked:** at random with a fixed seed, stratified by repo × difficulty, so the
  mix roughly matches the 455 eligible tasks:

| | Subset (60) | Eligible (455) |
|---|---|---|
| django / sympy / sphinx / matplotlib / scikit-learn | 27 / 9 / 5 / 5 / 4 | 209 / 68 / 39 / 34 / 31 |
| astropy / xarray / pytest / requests / pylint | 3 / 3 / 2 / 1 / 1 | 19 / 20 / 16 / 8 / 8 |
| seaborn / flask | 0 / 0 | 2 / 1 |
| `<15 min fix` / `15 min - 1 hour` | 26 / 34 | 194 / 261 |

The subset config also sets `n_concurrent_trials: 8` (8 tasks at a time); override with `-n`.
Modal's Shared Endpoint for Kimi-K3.1-Pro allowed 16 requests in flight at once in our workspace
(more get HTTP 429); each running task has at most one request in flight, so stay at 12 or below.

First run (`jobs/kimi-k31-pro-swv60/`, 2026-11-17): **53 of 60 passed (88.3%)**. Harbor's job
summary shows 89.8% because it leaves out `sphinx-doc__sphinx-10673`, which ended with
`RewardFileNotFoundError` (the grader produced no score). Took 57 minutes at 8 tasks at a time.
Inference cost **$9.66** (12.9M input tokens, 89% cached; 0.46M output tokens), $0.035–$0.47 per
task with a median of $0.14, matching Modal's bill.

To regenerate it, or to make a subset of a different size:

```bash
# Reproduces the committed file
uv run python scripts/sample_tasks.py --n 60 --seed 0 \
  --difficulty '<15 min fix' --difficulty '15 min - 1 hour' \
  --out configs/swe-bench-verified-60.yaml

# 100 tasks from all difficulty labels
uv run python scripts/sample_tasks.py --n 100 --seed 0 --out configs/swe-bench-verified-100.yaml
```

The script downloads the 500 task definitions from the Harbor registry (a few MB; no container
images) to read each task's repo and difficulty label. The registry sometimes drops connections
partway through; the script retries and fetches only the missing tasks.

### Other options

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

Example: on `swe-bench/django__django-11099` (a `<15 min fix` task), three runs all passed and cost
$0.090, $0.030 and $0.021 (12, 5 and 5 turns). For the first run, Modal's bill matched Harbor's token
counts exactly. Cost for the same task varies with the number of turns the model takes.

## Config notes

- **`interleaved_thinking: true`** — the endpoint returns reasoning in `reasoning_content`; this feeds
  it back on later turns, as Kimi expects in agent loops.
- **`session_id_headers: [Modal-Routing-Affinity-Key]`** — sends a per-trial ID in Modal's routing
  header so all turns of a trial hit the same container and reuse its prompt-prefix KV cache
  (76–86% of input tokens were cache hits in test runs). It doesn't configure the cache, only routing; it affects
  speed/cost, not results. (Harbor's docstring mentions `Modal-Session-ID`; that is for Modal
  *Servers* — Endpoints use `Modal-Routing-Affinity-Key`.)
- **`model_info`** — LiteLLM doesn't know this model, so the 1M-token context window (used for
  terminus-2's context summarization) and pricing are declared here.
- **Reasoning effort** — not set, so the endpoint default applies. Don't pass `--effort default`:
  Harbor sends the literal string `default`, which the endpoint doesn't accept.
