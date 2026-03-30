#!/usr/bin/env python3
"""
ownership_check.py — CUBE Registry ownership enforcement.

Validates that a PR author is permitted to modify each changed entry file.
Reads OWNERS.yaml from origin/main (NOT the PR branch) to prevent self-granted ownership.

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml


def read_owners_from_main() -> dict[str, list[str]]:
    """Read OWNERS.yaml from origin/main, not from the current branch."""
    try:
        result = subprocess.run(
            ["git", "show", "origin/main:OWNERS.yaml"],
            capture_output=True,
            text=True,
            check=True,
        )
        owners = yaml.safe_load(result.stdout) or {}
        return {k: list(v) for k, v in owners.items()}
    except subprocess.CalledProcessError as e:
        # If origin/main doesn't exist yet (brand-new repo), treat as empty
        if "does not exist" in e.stderr or "unknown revision" in e.stderr:
            print("::notice::OWNERS.yaml not found on origin/main — treating as empty (new repo)")
            return {}
        print(f"::error::Failed to read OWNERS.yaml from origin/main: {e.stderr}")
        sys.exit(1)


def entry_id_from_path(filepath: str) -> Optional[str]:
    """Extract benchmark ID from an entries/<id>.yaml path. Returns None if not an entry."""
    p = Path(filepath)
    if p.parts[0] == "entries" and p.suffix == ".yaml" and len(p.parts) == 2:
        return p.stem
    return None


def check_ownership(
    pr_author: str,
    changed_files: list[str],
    owners: dict[str, list[str]],
) -> bool:
    """
    Check ownership rules for all changed files.

    Returns True if all checks pass, False if any fail.
    """
    all_passed = True

    for filepath in changed_files:
        p = Path(filepath)

        # Block any direct modification of OWNERS.yaml
        if p.name == "OWNERS.yaml":
            print(
                f"::error file=OWNERS.yaml::OWNERS.yaml must not be modified in a PR. "
                f"It is maintained automatically by CI after merge."
            )
            print(
                f"❌ BLOCKED: OWNERS.yaml is CI-managed and cannot be modified in a PR."
            )
            all_passed = False
            continue

        # Block any modification of stress-results/
        if filepath.startswith("stress-results/") or p.parts[0] == "stress-results":
            print(
                f"::error file={filepath}::stress-results/ is CI-managed and cannot be "
                f"modified in a PR."
            )
            print(f"❌ BLOCKED: {filepath} is in stress-results/ which is CI-managed.")
            all_passed = False
            continue

        # For entry files, check ownership
        benchmark_id = entry_id_from_path(filepath)
        if benchmark_id is not None:
            allowed_authors = owners.get(benchmark_id)

            if allowed_authors is None:
                # New entry — no existing owner, open submission
                print(f"✅ PASS: {filepath} — new entry (not yet in OWNERS.yaml), open submission.")
                continue

            if pr_author in allowed_authors:
                print(
                    f"✅ PASS: {filepath} — @{pr_author} is a registered owner of '{benchmark_id}'."
                )
                continue
            else:
                owners_str = ", ".join(f"@{h}" for h in allowed_authors)
                print(
                    f"::error file={filepath}::@{pr_author} is not a registered owner of "
                    f"'{benchmark_id}'. Registered owners: {owners_str}. "
                    f"Only registered owners may modify an existing entry."
                )
                print(
                    f"❌ BLOCKED: @{pr_author} tried to modify '{benchmark_id}' "
                    f"but is not a registered owner. Owners: {owners_str}"
                )
                all_passed = False
                continue

        # Non-entry files outside protected areas are allowed (e.g. README, docs)
        print(f"ℹ️  SKIP: {filepath} — not an entry file, no ownership check needed.")

    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CUBE Registry ownership check. Validates PR author may modify each changed file."
    )
    parser.add_argument(
        "--pr-author",
        required=True,
        help="GitHub handle of the PR author (without @).",
    )
    parser.add_argument(
        "--changed-files",
        nargs="+",
        required=True,
        metavar="FILE",
        help="List of files changed in the PR (relative to repo root).",
    )
    args = parser.parse_args()

    pr_author: str = args.pr_author.lstrip("@")
    changed_files: list[str] = args.changed_files

    print(f"=== CUBE Registry Ownership Check ===")
    print(f"PR author: @{pr_author}")
    print(f"Changed files: {changed_files}")
    print()

    owners = read_owners_from_main()

    passed = check_ownership(pr_author, changed_files, owners)

    print()
    if passed:
        print("✅ Ownership check PASSED.")
        sys.exit(0)
    else:
        print("❌ Ownership check FAILED. See errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
