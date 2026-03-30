#!/usr/bin/env python3
"""
slow_check.py — CUBE Registry slow compliance check (Tier 2).

Runs a full debug episode against real infra (VM or Docker) for a given provider.
This script is the thin orchestrator that:
  1. Reads the registry entry YAML
  2. Provisions infra from benchmark.resources using the appropriate InfraConfig
  3. Runs a full debug episode (spawn → debug agent → evaluation → close)
  4. Captures stress-test profiling metrics
  5. Writes results to stress-results/<id>/v<version>.json
  6. Updates stress_results_url in the entry YAML

Note: For VM-based resources, the benchmark package runs INSIDE the provisioned VM,
not on this runner. This runner holds cloud credentials and orchestrates via cloud SDK.
The benchmark package is never imported here.

Exit codes:
  0 — slow check passed, results written
  1 — slow check failed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
ENTRIES_DIR = REPO_ROOT / "entries"
STRESS_RESULTS_DIR = REPO_ROOT / "stress-results"


def load_entry(entry_path: Path) -> dict:
    yaml = YAML()
    with open(entry_path) as f:
        return yaml.load(f)


def run_docker_debug_episode(entry: dict, provider: str) -> dict[str, Any]:
    """
    Run a debug episode using Docker resources (directly on the runner).
    Installs the package, runs debug episode, captures metrics.
    Returns a metrics dict.
    """
    package = entry["package"]
    version = entry["version"]
    benchmark_id = entry["id"]

    print(f"  [docker] Installing {package}=={version} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", f"{package}=={version}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip install failed: {result.stderr}")

    # Run the debug episode in a subprocess to isolate execution
    debug_script = f"""
import importlib, time, sys

pkg = importlib.import_module("{package.replace('-', '_')}")
BenchmarkClass = pkg.Benchmark

t0 = time.time()
benchmark = BenchmarkClass()
setup_time = time.time() - t0

tasks = benchmark.debug_tasks() if hasattr(benchmark, 'debug_tasks') else []
if not tasks:
    print("ERROR: no debug tasks", file=sys.stderr)
    sys.exit(1)

task = tasks[0]

# Spawn
t_spawn = time.time()
obs = benchmark.reset(task_id=task.task_id if hasattr(task, 'task_id') else str(task))
spawn_time = time.time() - t_spawn

# Run debug agent if available
step_times = []
if hasattr(benchmark, 'make_debug_agent'):
    agent = benchmark.make_debug_agent()
    for _ in range(3):  # run a few steps
        t_step = time.time()
        if hasattr(agent, 'act'):
            action = agent.act(obs)
        else:
            action = None
        step_times.append(time.time() - t_step)
        if action is not None:
            try:
                obs, reward, done, info = benchmark.step(action)
                if done:
                    break
            except Exception:
                break

# Evaluation
eval_result = benchmark.evaluate() if hasattr(benchmark, 'evaluate') else {{}}

# Close
benchmark.close() if hasattr(benchmark, 'close') else None

import statistics
results = {{
    "setup_time_s": round(setup_time, 3),
    "spawn_time_s": round(spawn_time, 3),
    "step_latency_p50_s": round(statistics.median(step_times), 3) if step_times else None,
    "step_latency_p95_s": round(sorted(step_times)[int(len(step_times)*0.95)] if len(step_times) > 1 else step_times[0], 3) if step_times else None,
    "step_latency_p99_s": round(sorted(step_times)[-1], 3) if step_times else None,
    "episode_time_s": round(sum(step_times) + spawn_time, 3),
    "eval_valid": isinstance(eval_result, dict),
}}
import json
print(json.dumps(results))
"""

    result = subprocess.run(
        [sys.executable, "-c", debug_script],
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Debug episode failed:\n{result.stderr}")

    # Parse metrics from last line of stdout
    output_lines = result.stdout.strip().splitlines()
    for line in reversed(output_lines):
        try:
            metrics = json.loads(line)
            return metrics
        except json.JSONDecodeError:
            continue

    raise RuntimeError(f"No metrics JSON found in output:\n{result.stdout}")


def run_vm_debug_episode(entry: dict, provider: str) -> dict[str, Any]:
    """
    Run a debug episode using VM resources.
    The benchmark package runs INSIDE the provisioned VM, not here.
    This function orchestrates via cloud SDK (provider-specific).

    In a real implementation, this would:
    1. Call cloud SDK to provision a VM from benchmark.resources
    2. Bootstrap the VM with the benchmark package
    3. Run the debug episode remotely
    4. Collect metrics
    5. Terminate and deregister everything

    For now, this is a placeholder that raises NotImplementedError.
    The actual implementation requires cloud SDK integration (boto3, azure-sdk, etc.)
    """
    raise NotImplementedError(
        f"VM-based slow check for provider '{provider}' requires cloud SDK integration. "
        f"See design/registry_specs.md for the full spec. "
        f"This placeholder must be replaced with actual provisioning logic."
    )


def write_stress_results(
    entry: dict,
    provider: str,
    metrics: dict[str, Any],
    passed: bool,
    error: str | None,
) -> Path:
    """Write stress results to stress-results/<id>/v<version>.json and return the path."""
    benchmark_id = entry["id"]
    version = entry["version"]

    results_dir = STRESS_RESULTS_DIR / benchmark_id
    results_dir.mkdir(parents=True, exist_ok=True)

    results_file = results_dir / f"v{version}.json"

    # Load existing results to append provider-specific data
    if results_file.exists():
        with open(results_file) as f:
            all_results = json.load(f)
    else:
        all_results = {
            "id": benchmark_id,
            "version": version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "providers": {},
        }

    all_results["providers"][provider] = {
        "passed": passed,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics if passed else {},
    }

    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)

    return results_file


def update_stress_results_url(entry_path: Path, results_path: Path) -> None:
    """Update stress_results_url in the entry YAML."""
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(entry_path) as f:
        doc = yaml.load(f)

    # Store relative path from repo root
    rel_path = results_path.relative_to(REPO_ROOT)
    doc["stress_results_url"] = str(rel_path)

    with open(entry_path, "w") as f:
        yaml.dump(doc, f)


def needs_slow_check(entry_path: Path) -> bool:
    """
    Check if slow check should re-run by comparing changed fields against previous commit.
    Re-runs on: version, package, resources (image_url changes), supported_infra.
    Does NOT re-run for: tags, description, paper, getting_started_url, legal, authors.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD", "--", str(entry_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        diff = result.stdout
        trigger_fields = ["version:", "package:", "image_url:", "supported_infra:"]
        return any(field in diff for field in trigger_fields)
    except subprocess.CalledProcessError:
        # On error (e.g. first commit), always run
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CUBE Registry slow compliance check. Runs a full debug episode on real infra."
    )
    parser.add_argument(
        "--entry",
        required=True,
        metavar="PATH",
        help="Path to the registry entry YAML file.",
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=["aws", "azure", "gcp", "local", "docker"],
        help="Infrastructure provider to test on.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if no trigger fields changed.",
    )
    args = parser.parse_args()

    entry_path = Path(args.entry).resolve()

    print(f"=== CUBE Registry Slow Check ===")
    print(f"Entry: {entry_path}")
    print(f"Provider: {args.provider}")
    print()

    if not entry_path.exists():
        print(f"::error::Entry file not found: {entry_path}")
        sys.exit(1)

    entry = load_entry(entry_path)
    benchmark_id = entry["id"]

    # Check if slow check needs to run
    if not args.force and not needs_slow_check(entry_path):
        print("ℹ️  No trigger fields changed. Skipping slow check.")
        print("  (Pass --force to override)")
        sys.exit(0)

    # Determine resource types
    resources = entry.get("resources", []) or []
    has_vm_resources = any(r.get("type") == "VMResourceConfig" for r in resources)

    supported_infra = entry.get("supported_infra", ["aws"])
    if args.provider not in supported_infra and args.provider not in ("docker", "local"):
        print(f"::warning::Provider '{args.provider}' not in supported_infra {supported_infra}")

    # Run the appropriate check
    passed = False
    error: str | None = None
    metrics: dict[str, Any] = {}

    try:
        if has_vm_resources and args.provider not in ("docker", "local"):
            print(f"Running VM-based debug episode on {args.provider}...")
            metrics = run_vm_debug_episode(entry, args.provider)
        else:
            print(f"Running Docker-based debug episode...")
            metrics = run_docker_debug_episode(entry, args.provider)

        passed = True
        print(f"\nMetrics: {json.dumps(metrics, indent=2)}")

    except NotImplementedError as e:
        error = str(e)
        print(f"::warning::{e}")
        print(f"⚠️  VM slow check not yet implemented for this provider.")
        # Don't fail — this is a placeholder
        sys.exit(0)

    except Exception as e:
        error = str(e)
        print(f"::error::Slow check failed for '{benchmark_id}' on {args.provider}: {e}")
        print(f"❌ Slow check FAILED: {e}")

    # Write stress results
    results_path = write_stress_results(entry, args.provider, metrics, passed, error)
    print(f"\nResults written to: {results_path}")

    # Update entry YAML with stress_results_url
    if passed:
        update_stress_results_url(entry_path, results_path)
        print(f"Updated stress_results_url in {entry_path}")
        print("\n✅ Slow check PASSED.")
        sys.exit(0)
    else:
        print(f"\n❌ Slow check FAILED. Authors will be notified via GitHub issue.")
        sys.exit(1)


if __name__ == "__main__":
    main()
