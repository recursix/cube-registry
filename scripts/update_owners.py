#!/usr/bin/env python3
"""
update_owners.py — Update OWNERS.yaml after a successful entry merge.

Reads authors[].github from the entry YAML and adds/updates the mapping in OWNERS.yaml.
Run by the update-owners workflow after a successful PR merge.

Exit codes:
  0 — OWNERS.yaml updated (or already up to date)
  1 — error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
OWNERS_PATH = REPO_ROOT / "OWNERS.yaml"


def load_yaml_file(path: Path) -> CommentedMap:
    """Load a YAML file, returning a CommentedMap (preserves structure and comments)."""
    yaml = YAML()
    yaml.preserve_quotes = True
    if not path.exists():
        return CommentedMap()
    with open(path) as f:
        data = yaml.load(f)
    return data if data is not None else CommentedMap()


def update_owners(entry_path: Path) -> bool:
    """
    Read entry YAML, extract github handles from authors[], update OWNERS.yaml.
    Returns True if OWNERS.yaml was modified, False if already up to date.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.width = 200

    # Load entry
    entry_data = load_yaml_file(entry_path)
    benchmark_id: str = entry_data.get("id", "")
    if not benchmark_id:
        raise ValueError(f"Entry at {entry_path} has no 'id' field")

    authors = entry_data.get("authors", []) or []
    github_handles: list[str] = []
    for author in authors:
        handle = author.get("github", "")
        if handle and handle not in github_handles:
            github_handles.append(handle)

    if not github_handles:
        raise ValueError(f"Entry '{benchmark_id}' has no authors with github handles")

    print(f"Entry: {benchmark_id}")
    print(f"Authors: {github_handles}")

    # Load existing OWNERS.yaml
    owners_data = load_yaml_file(OWNERS_PATH)

    # Check if update needed
    current = list(owners_data.get(benchmark_id, []) or [])
    new_handles = sorted(set(current) | set(github_handles))

    if sorted(current) == new_handles:
        print(f"OWNERS.yaml already up to date for '{benchmark_id}'.")
        return False

    # Update
    owners_data[benchmark_id] = new_handles

    # Write back, preserving the header comment
    with open(OWNERS_PATH, "w") as f:
        # Write header comment if OWNERS.yaml is new or empty
        if len(owners_data) == 1 and benchmark_id in owners_data:
            f.write("# OWNERS.yaml — do not edit by hand, maintained by CI\n")
            f.write("# Maps benchmark id → list of GitHub handles that may modify that entry.\n")
            f.write("# Updated automatically by the update-owners workflow after a successful merge.\n")
        yaml.dump(owners_data, f)

    added = set(new_handles) - set(current)
    print(f"Updated OWNERS.yaml: '{benchmark_id}' → {new_handles}")
    if added:
        print(f"  Added new handles: {sorted(added)}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update OWNERS.yaml after a registry entry is merged."
    )
    parser.add_argument(
        "--entry",
        required=True,
        metavar="PATH",
        help="Path to the merged registry entry YAML file.",
    )
    args = parser.parse_args()

    entry_path = Path(args.entry).resolve()
    if not entry_path.exists():
        print(f"::error::Entry file not found: {entry_path}")
        sys.exit(1)

    print(f"=== Update OWNERS.yaml ===")
    print(f"Entry: {entry_path}")
    print()

    try:
        modified = update_owners(entry_path)
    except ValueError as e:
        print(f"::error::{e}")
        sys.exit(1)
    except Exception as e:
        print(f"::error::Unexpected error: {e}")
        sys.exit(1)

    if modified:
        print("\n✅ OWNERS.yaml updated.")
    else:
        print("\nℹ️  OWNERS.yaml unchanged (already up to date).")

    sys.exit(0)


if __name__ == "__main__":
    main()
