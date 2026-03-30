#!/usr/bin/env python3
"""
health_check.py — CUBE Registry periodic health check.

For each entry in entries/*.yaml:
  1. Verify pip install works (package not yanked/removed)
  2. HTTP HEAD all resource image_url values (still reachable)
  3. HTTP HEAD benchmark_license.source_url (still reachable)

On failure: prints which entries failed and why.
Exit 1 if any failures (workflow opens GitHub issues).

Exit codes:
  0 — all entries healthy
  1 — one or more entries degraded
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from ruamel.yaml import YAML

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
ENTRIES_DIR = REPO_ROOT / "entries"

HTTP_TIMEOUT = 15  # seconds
HTTP_HEAD_USER_AGENT = "cube-registry-health-check/1.0 (+https://github.com/The-AI-Alliance/cube-registry)"


class HealthResult(NamedTuple):
    entry_id: str
    passed: bool
    failures: list[str]


def pip_installable(package: str, version: str) -> tuple[bool, str]:
    """
    Check if package==version is installable from PyPI (dry-run, no actual install).
    Returns (ok, error_message).
    """
    pkg_spec = f"{package}=={version}"
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--quiet",
                "--no-deps",  # we only care about the package itself
                pkg_spec,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False, f"pip install --dry-run failed: {result.stderr.strip()}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "pip install check timed out"
    except Exception as e:
        return False, str(e)


def http_head(url: str) -> tuple[bool, str]:
    """
    HTTP HEAD request to verify a URL is reachable.
    Returns (ok, error_message).
    """
    try:
        req = Request(url, method="HEAD")
        req.add_header("User-Agent", HTTP_HEAD_USER_AGENT)
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
            if 200 <= status < 400:
                return True, ""
            return False, f"HTTP {status}"
    except HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except URLError as e:
        return False, f"URL error: {e.reason}"
    except Exception as e:
        return False, str(e)


def check_entry(entry_path: Path) -> HealthResult:
    """Run all health checks for a single entry. Returns HealthResult."""
    yaml = YAML()
    with open(entry_path) as f:
        entry = yaml.load(f)

    entry_id = entry.get("id", entry_path.stem)
    failures: list[str] = []

    # Skip archived entries
    if entry.get("status") == "archived":
        print(f"  [{entry_id}] SKIPPED (archived)")
        return HealthResult(entry_id=entry_id, passed=True, failures=[])

    package = entry.get("package", "")
    version = entry.get("version", "")

    # Check 1: pip installable
    if package and version:
        ok, err = pip_installable(package, version)
        if ok:
            print(f"  [{entry_id}] pip install {package}=={version} ✅")
        else:
            msg = f"pip install {package}=={version} failed: {err}"
            print(f"  [{entry_id}] {msg} ❌")
            failures.append(msg)
    else:
        failures.append("Missing package or version field")

    # Check 2: resource image URLs
    resources = entry.get("resources", []) or []
    for i, resource in enumerate(resources):
        image_url = resource.get("image_url")
        if not image_url:
            continue
        ok, err = http_head(image_url)
        if ok:
            print(f"  [{entry_id}] resource[{i}] image_url ✅ {image_url}")
        else:
            msg = f"Resource image URL unreachable: {image_url} — {err}"
            print(f"  [{entry_id}] {msg} ❌")
            failures.append(msg)

    # Check 3: benchmark license source URL
    legal = entry.get("legal", {}) or {}
    bench_license = legal.get("benchmark_license", {}) or {}
    source_url = bench_license.get("source_url")
    if source_url:
        ok, err = http_head(source_url)
        if ok:
            print(f"  [{entry_id}] benchmark_license.source_url ✅")
        else:
            msg = f"License source URL unreachable: {source_url} — {err}"
            print(f"  [{entry_id}] {msg} ❌")
            failures.append(msg)

    passed = len(failures) == 0
    return HealthResult(entry_id=entry_id, passed=passed, failures=failures)


def set_status_degraded(entry_path: Path) -> None:
    """Set status: degraded in the entry YAML."""
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(entry_path) as f:
        doc = yaml.load(f)
    doc["status"] = "degraded"
    with open(entry_path, "w") as f:
        yaml.dump(doc, f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CUBE Registry periodic health check. Checks all entries for package availability and URL reachability."
    )
    parser.add_argument(
        "--entry",
        metavar="PATH",
        default=None,
        help="Check only this entry (default: check all entries/*.yaml).",
    )
    parser.add_argument(
        "--update-status",
        action="store_true",
        help="Write 'status: degraded' to YAML for failing entries.",
    )
    args = parser.parse_args()

    print("=== CUBE Registry Health Check ===")
    print()

    if args.entry:
        entry_paths = [Path(args.entry).resolve()]
    else:
        entry_paths = sorted(ENTRIES_DIR.glob("*.yaml"))
        # Exclude .gitkeep and non-entry files
        entry_paths = [p for p in entry_paths if p.stem != ".gitkeep"]

    if not entry_paths:
        print("No entries found.")
        sys.exit(0)

    print(f"Checking {len(entry_paths)} entry/entries...\n")

    results: list[HealthResult] = []
    for entry_path in entry_paths:
        result = check_entry(entry_path)
        results.append(result)
        if not result.passed and args.update_status:
            set_status_degraded(entry_path)
            print(f"  [{result.entry_id}] status set to 'degraded'")

    # Summary
    print()
    print("=== Health Check Summary ===")
    failed = [r for r in results if not r.passed]
    passed = [r for r in results if r.passed]

    print(f"✅ Healthy: {len(passed)}")
    print(f"❌ Degraded: {len(failed)}")

    if failed:
        print()
        print("Failed entries:")
        for r in failed:
            print(f"  {r.entry_id}:")
            for f in r.failures:
                print(f"    • {f}")
            # GitHub Actions annotation for issue creation
            print(f"::error::Health check failed for '{r.entry_id}': {'; '.join(r.failures)}")
        sys.exit(1)
    else:
        print("\n✅ All entries healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
