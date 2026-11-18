"""Draw a fixed, stratified sample of SWE-bench Verified tasks and write a Harbor config.

The sample is stratified by (repo, difficulty) with largest-remainder allocation, so it
keeps roughly the same repo and difficulty mix as the tasks it is drawn from. A fixed seed
makes it reproducible: rerunning with the same arguments gives the same task list.

Difficulty labels in SWE-bench Verified (estimated time for a human to fix):
"<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours".

Usage:
    # The committed 60-task subset (only tasks labelled under 1 hour)
    uv run python scripts/sample_tasks.py --n 60 --seed 0 \
        --difficulty "<15 min fix" --difficulty "15 min - 1 hour"

    # All difficulties, different size
    uv run python scripts/sample_tasks.py --n 100 --seed 0 --out configs/swe-bench-verified-100.yaml
"""

from __future__ import annotations

import argparse
import collections
import random
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

DATASET = "swe-bench/swe-bench-verified"


def load_tasks(task_dir: Path) -> list[tuple[str, str, str]]:
    """Return (task name, repo, difficulty) for every task.toml under task_dir."""
    tasks = []
    for toml_path in sorted(task_dir.rglob("task.toml")):
        data = tomllib.loads(toml_path.read_text())
        name = data["task"]["name"]  # e.g. swe-bench/django__django-11099
        repo = name.split("/", 1)[1].split("__", 1)[0]
        difficulty = data.get("metadata", {}).get("difficulty", "unknown")
        tasks.append((name, repo, difficulty))
    return tasks


def stratified_sample(tasks, n: int, seed: int) -> list[str]:
    strata: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for name, repo, difficulty in tasks:
        strata[(repo, difficulty)].append(name)

    total = len(tasks)
    quotas = {k: n * len(v) / total for k, v in strata.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    # Largest remainder; ties broken by stratum key so the result is deterministic.
    leftovers = sorted(strata, key=lambda k: (-(quotas[k] - alloc[k]), k))
    for k in leftovers[: n - sum(alloc.values())]:
        alloc[k] += 1

    rng = random.Random(seed)
    picked = []
    for k in sorted(strata):
        picked += rng.sample(sorted(strata[k]), alloc[k])
    return sorted(picked)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=60)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=Path("configs/swe-bench-verified-60.yaml"))
    parser.add_argument(
        "--difficulty",
        action="append",
        help="Only sample tasks with this difficulty label (repeatable; default: all)",
    )
    parser.add_argument(
        "--task-dir",
        type=Path,
        help="Already-downloaded dataset directory (default: download a fresh copy)",
    )
    args = parser.parse_args()

    if args.task_dir:
        tasks = load_tasks(args.task_dir)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            # The registry sometimes drops connections partway through. Re-running the
            # download into the same directory fetches only the tasks still missing.
            for attempt in range(1, 6):
                result = subprocess.run(
                    ["harbor", "dataset", "download", DATASET, "-o", tmp],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode == 0:
                    break
                print(f"Download attempt {attempt} incomplete, retrying...", file=sys.stderr)
                time.sleep(5)
            else:
                sys.exit(f"Dataset download failed:\n{result.stderr[-2000:]}")
            tasks = load_tasks(Path(tmp))

    if args.difficulty:
        labels = {d for _, _, d in tasks}
        unknown = set(args.difficulty) - labels
        if unknown:
            sys.exit(f"Unknown difficulty {sorted(unknown)}; labels are {sorted(labels)}")
        tasks = [t for t in tasks if t[2] in args.difficulty]

    if len(tasks) < args.n:
        sys.exit(f"Only {len(tasks)} tasks match, cannot sample {args.n}")

    picked = stratified_sample(tasks, args.n, args.seed)
    info = {name: (repo, diff) for name, repo, diff in tasks}
    repos = collections.Counter(info[t][0] for t in picked)
    diffs = collections.Counter(info[t][1] for t in picked)

    cmd = ["uv", "run", "python", "scripts/sample_tasks.py", "--n", str(args.n),
           "--seed", str(args.seed)]
    for d in args.difficulty or []:
        cmd += ["--difficulty", d]
    cmd += ["--out", str(args.out)]
    pool = "difficulty " + " / ".join(args.difficulty) if args.difficulty else "all difficulties"

    lines = [
        f"# {args.n} SWE-bench Verified tasks drawn from {len(tasks)} ({pool}),",
        f"# stratified by repo x difficulty (seed {args.seed}).",
        "# Generated by scripts/sample_tasks.py -- regenerate instead of editing by hand:",
        "#   " + shlex.join(cmd),
        "#",
        "# Use together with the agent config:",
        f"#   scripts/run-eval.sh -c {args.out} --job-name <name>",
        "#",
        "# Repos:      " + ", ".join(f"{r} {c}" for r, c in repos.most_common()),
        "# Difficulty: " + ", ".join(f"{d} {c}" for d, c in diffs.most_common()),
        "",
        "n_concurrent_trials: 8",
        "",
        "datasets:",
        f"  - name: {DATASET}",
        "    task_names:",
        *[f"      - {t}" for t in picked],
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines))
    print(f"Wrote {len(picked)} tasks to {args.out}")
    print("Repos:     ", dict(repos.most_common()))
    print("Difficulty:", dict(diffs.most_common()))


if __name__ == "__main__":
    main()
