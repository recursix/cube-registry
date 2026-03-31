#!/usr/bin/env python3
"""
quick_check.py — CUBE Registry quick compliance check (Tier 1).

Validates a registry entry YAML and introspects the benchmark package:
  1. Validate YAML against registry-schema.json
  2. pip install the package in a subprocess (isolated)
  3. Import the package, find the Benchmark class
  4. Instantiate Benchmark(), call basic API methods
  5. Introspect benchmark.resources, task class for features
  6. Write back CI-derived fields to the YAML

Exit codes:
  0 — all checks passed, YAML updated with CI-derived fields
  1 — one or more checks failed
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import jsonschema
from ruamel.yaml import YAML

# Resolve paths relative to this script's location
SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
SCHEMA_PATH = REPO_ROOT / "registry-schema.json"
KNOWN_AUTHORS_PATH = REPO_ROOT / "known-authors.yaml"


def load_schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def load_yaml(path: Path) -> dict:
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f)


def load_known_authors() -> dict[str, list[str]]:
    if not KNOWN_AUTHORS_PATH.exists():
        return {}
    yaml = YAML()
    with open(KNOWN_AUTHORS_PATH) as f:
        data = yaml.load(f) or {}
    return {k: list(v) for k, v in data.items()}


def validate_schema(entry: dict, schema: dict) -> list[str]:
    """Validate entry against schema. Returns list of error messages."""
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(entry), key=lambda e: list(e.path))
    return [f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def pip_install_package(package: str, version: str, dev_install_url: str | None = None) -> tuple[bool, str]:
    """Install package==version from PyPI, falling back to dev_install_url if PyPI fails."""
    pkg_spec = f"{package}=={version}"
    print(f"  Installing {pkg_spec} ...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", pkg_spec],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return True, ""
        pypi_err = result.stderr
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 5 minutes"
    except Exception as e:
        return False, str(e)

    # PyPI failed — try dev_install_url if provided
    if dev_install_url:
        print(f"  ::warning::PyPI install failed. Trying dev_install_url: {dev_install_url}")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", dev_install_url],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                print(f"  Installed from dev_install_url (package not yet on PyPI).")
                return True, ""
            return False, f"PyPI failed:\n{pypi_err}\n\ndev_install_url also failed:\n{result.stderr}"
        except subprocess.TimeoutExpired:
            return False, "dev_install_url install timed out after 10 minutes"
        except Exception as e:
            return False, f"PyPI failed: {pypi_err}\ndev_install_url error: {e}"

    return False, f"pip install failed:\n{pypi_err}"


def find_benchmark_class(package: str) -> tuple[Any | None, str]:
    """Resolve the Benchmark class for *package*.

    Resolution order:
    1. ``cube.benchmarks`` entry point matching the package name.
    2. A class literally named ``Benchmark`` exported at the package top level.
    """
    try:
        eps = importlib.metadata.entry_points(group="cube.benchmarks")
        matched = [ep for ep in eps if ep.name == package]
        if matched:
            try:
                cls = matched[0].load()
                return cls, ""
            except Exception as e:
                return None, f"Entry point '{matched[0].value}' failed to load: {e}"
    except Exception:
        pass

    try:
        mod = importlib.import_module(package.replace("-", "_"))
    except ImportError as e:
        return None, f"Could not import package '{package}': {e}"

    benchmark_cls = getattr(mod, "Benchmark", None)
    if benchmark_cls is None:
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name, None)
            if attr is not None and inspect.isclass(attr) and attr.__name__ == "Benchmark":
                benchmark_cls = attr
                break

    if benchmark_cls is None:
        return None, (
            f"Package '{package}' has no 'cube.benchmarks' entry point and does not export "
            f"a class named 'Benchmark'."
        )

    return benchmark_cls, ""


def _serialize_resource(r: Any) -> dict:
    if hasattr(r, "model_dump"):
        d = r.model_dump()
    elif hasattr(r, "dict"):
        d = r.dict()
    else:
        d = vars(r)
    d["type"] = type(r).__name__
    return d


def introspect_benchmark(benchmark_cls: Any, package: str) -> dict[str, Any]:
    """
    Introspect the Benchmark class and its package module to derive CI fields.

    Follows the CUBE debug-module convention:
      - task_count: via benchmark.get_task_configs() (not .tasks())
      - has_debug_task: module exposes get_debug_benchmark()
      - has_debug_agent: module exposes make_debug_agent()
      - resources: benchmark.resources (present on VM/Docker benchmarks)
      - features: introspected from the task class
    """
    derived: dict[str, Any] = {}

    # Import the package module for debug-interface checks
    try:
        mod = importlib.import_module(package.replace("-", "_"))
    except ImportError:
        mod = None

    # Instantiate benchmark
    try:
        benchmark = benchmark_cls()
    except Exception as e:
        raise RuntimeError(f"Failed to instantiate Benchmark(): {e}") from e

    # --- task_count: use get_task_configs() (the real CUBE API) ---
    task_cls = None
    try:
        configs = list(benchmark.get_task_configs())
        derived["task_count"] = len(configs)
        if configs:
            # Instantiate a task to detect features; don't actually spawn
            task_cls = benchmark.task_config_class if hasattr(benchmark, "task_config_class") else None
            # Try to get the task class from the config's make() result type annotation
            if task_cls is None and hasattr(configs[0], "make"):
                import typing
                hints = typing.get_type_hints(configs[0].make) if hasattr(configs[0].make, "__annotations__") else {}
                task_cls = hints.get("return")
        print(f"  task_count: {derived['task_count']}")
    except Exception as e:
        print(f"  ::warning::Could not call benchmark.get_task_configs(): {e}")
        derived["task_count"] = None

    # --- has_debug_task: module-level get_debug_benchmark() (CUBE debug convention) ---
    has_debug_task = False
    if mod is not None and callable(getattr(mod, "get_debug_benchmark", None)):
        try:
            debug_b = mod.get_debug_benchmark()
            debug_configs = list(debug_b.get_task_configs())
            has_debug_task = len(debug_configs) > 0
            print(f"  has_debug_task: {has_debug_task} ({len(debug_configs)} debug task(s))")
        except Exception as e:
            print(f"  ::warning::get_debug_benchmark() failed: {e}")
    else:
        print(f"  has_debug_task: False (no get_debug_benchmark() in module)")
    derived["has_debug_task"] = has_debug_task

    # --- has_debug_agent: module-level make_debug_agent() ---
    derived["has_debug_agent"] = mod is not None and callable(getattr(mod, "make_debug_agent", None))
    print(f"  has_debug_agent: {derived['has_debug_agent']}")

    # --- resources: present on VM/Docker benchmarks ---
    try:
        resources_list = benchmark.resources if hasattr(benchmark, "resources") else []
        if not isinstance(resources_list, list):
            resources_list = list(resources_list)
        derived["resources"] = [_serialize_resource(r) for r in resources_list]
        print(f"  resources: {len(derived['resources'])} resource(s)")
    except Exception as e:
        print(f"  ::warning::Could not introspect benchmark.resources: {e}")
        derived["resources"] = []

    # --- features ---
    features: dict[str, bool] = {
        "async": False,
        "streaming": False,
        "multi_agent": False,
        "multi_dim_reward": False,
    }

    if task_cls is not None:
        # Check for async_step / async_reset overrides
        for method_name in ("async_step", "async_reset"):
            method = getattr(task_cls, method_name, None)
            if method is not None and inspect.iscoroutinefunction(method):
                features["async"] = True
                break

        # Check for stream_action override
        stream_method = getattr(task_cls, "stream_action", None)
        if stream_method is not None:
            # Check it's overridden (not just inherited from base)
            for base in task_cls.__mro__[1:]:
                base_stream = getattr(base, "stream_action", None)
                if base_stream is not None and stream_method is not base_stream:
                    features["streaming"] = True
                    break

        # Check for MultiAgentTask subclass
        for base in inspect.getmro(task_cls):
            if base.__name__ == "MultiAgentTask":
                features["multi_agent"] = True
                break

    derived["features"] = features
    print(f"  features: {features}")

    # --- action_space ---
    # This is typically derived from a reset task; we do a best-effort introspection
    derived["action_space"] = []
    try:
        if task_cls is not None and hasattr(task_cls, "tools"):
            tools_attr = getattr(task_cls, "tools", None)
            if callable(tools_attr):
                tools_attr = tools_attr()
            if isinstance(tools_attr, list):
                for tool in tools_attr:
                    if hasattr(tool, "name"):
                        entry = {"name": tool.name}
                        if hasattr(tool, "description"):
                            entry["description"] = tool.description
                        derived["action_space"].append(entry)
    except Exception as e:
        print(f"  ::notice::Could not introspect action_space: {e}")

    return derived


def check_verified_by_original_authors(
    entry: dict, pr_author: str | None, known_authors: dict[str, list[str]]
) -> bool:
    """
    Returns True if the PR author or any entry author appears in known-authors.yaml
    for this benchmark ID.
    """
    benchmark_id = entry.get("id", "")
    known = known_authors.get(benchmark_id, [])
    if not known:
        return False

    entry_github_handles = [a.get("github", "") for a in entry.get("authors", [])]
    all_handles = entry_github_handles
    if pr_author:
        all_handles = all_handles + [pr_author]

    return any(h in known for h in all_handles)


def write_derived_fields(entry_path: Path, entry: dict, derived: dict, pr_author: str | None) -> None:
    """
    Write CI-derived fields back to the YAML file, preserving comments.
    Sets status to 'active' if not already set to 'archived'.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.best_sequence_indent = 2
    yaml.best_sequence_dash_offset = 2
    yaml.width = 120

    with open(entry_path) as f:
        doc = yaml.load(f)

    # Status: only set if not already 'archived'
    if doc.get("status") != "archived":
        doc["status"] = "active"

    # Core CI-derived fields
    for field in ("resources", "task_count", "has_debug_task", "has_debug_agent",
                  "action_space", "features"):
        if derived.get(field) is not None:
            doc[field] = derived[field]

    # Cleanup: remove any benchmark_license block that has no 'reported' field
    # (could have been written by an older version of this script).
    legal = doc.get("legal")
    if isinstance(legal, dict):
        bench_lic = legal.get("benchmark_license")
        if bench_lic is not None and not (bench_lic or {}).get("reported"):
            del legal["benchmark_license"]

    # verified_by_original_authors — only set when benchmark_license.reported already exists.
    known_authors = load_known_authors()
    verified = check_verified_by_original_authors(doc, pr_author, known_authors)
    bench_lic = (doc.get("legal") or {}).get("benchmark_license")
    if bench_lic and bench_lic.get("reported"):
        bench_lic["verified_by_original_authors"] = verified

    with open(entry_path, "w") as f:
        yaml.dump(doc, f)

    print(f"  Written CI-derived fields to {entry_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CUBE Registry quick compliance check. Validates entry YAML and introspects the package."
    )
    parser.add_argument(
        "--entry",
        required=True,
        metavar="PATH",
        help="Path to the registry entry YAML file (e.g. entries/osworld.yaml).",
    )
    parser.add_argument(
        "--pr-author",
        default=None,
        metavar="HANDLE",
        help="GitHub handle of the PR author (used for verified_by_original_authors check).",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Skip pip install (use already-installed package). Useful for testing.",
    )
    args = parser.parse_args()

    entry_path = Path(args.entry).resolve()
    pr_author: str | None = args.pr_author.lstrip("@") if args.pr_author else None

    print(f"=== CUBE Registry Quick Check ===")
    print(f"Entry: {entry_path}")
    print()

    # --- Step 1: Load and validate YAML ---
    print("Step 1: Schema validation")
    try:
        entry = load_yaml(entry_path)
    except Exception as e:
        print(f"::error file={entry_path}::Failed to parse YAML: {e}")
        sys.exit(1)

    schema = load_schema()
    errors = validate_schema(entry, schema)
    if errors:
        for err in errors:
            print(f"::error file={entry_path}::Schema error: {err}")
        print(f"❌ Schema validation FAILED ({len(errors)} error(s)):")
        for err in errors:
            print(f"   • {err}")
        sys.exit(1)
    print("  ✅ Schema valid")

    # --- Step 1b: Verify id matches filename ---
    expected_id = entry_path.stem
    if entry.get("id") != expected_id:
        print(
            f"::error file={entry_path}::Entry 'id' field ('{entry.get('id')}') "
            f"does not match filename ('{expected_id}.yaml')."
        )
        print(f"❌ Entry id mismatch: expected '{expected_id}', got '{entry.get('id')}'")
        sys.exit(1)
    print(f"  ✅ Entry id matches filename: '{expected_id}'")

    package = entry["package"]
    version = entry["version"]

    # --- Step 2: pip install ---
    print(f"\nStep 2: Package installation")
    dev_install_url: str | None = entry.get("dev_install_url")
    if args.no_install:
        print("  Skipping pip install (--no-install flag set)")
    else:
        ok, err = pip_install_package(package, version, dev_install_url=dev_install_url)
        if not ok:
            print(f"::error file={entry_path}::Package install failed: {err}")
            print(f"❌ pip install FAILED:\n{err}")
            sys.exit(1)
        print(f"  ✅ {package}=={version} installed")

    # --- Step 3: Import and find Benchmark class ---
    print(f"\nStep 3: Import benchmark")
    benchmark_cls, err = find_benchmark_class(package)
    if benchmark_cls is None:
        print(f"::error file={entry_path}::Import failed: {err}")
        print(f"❌ Import FAILED: {err}")
        sys.exit(1)
    print(f"  ✅ Found Benchmark class: {benchmark_cls}")

    # --- Step 4: Introspect benchmark ---
    print(f"\nStep 4: Introspect benchmark")
    try:
        derived = introspect_benchmark(benchmark_cls, package)
    except RuntimeError as e:
        print(f"::error file={entry_path}::Benchmark introspection failed: {e}")
        print(f"❌ Introspection FAILED: {e}")
        sys.exit(1)

    # --- Validate has_debug_task requirement ---
    if not derived.get("has_debug_task", False):
        print(
            f"::error file={entry_path}::Benchmark has no debug tasks. "
            f"At least one debug task is required for slow check to run."
        )
        print("❌ FAILED: no debug tasks declared (required for slow check)")
        sys.exit(1)
    print("  ✅ Debug task present")

    # --- Step 5: Write back CI-derived fields ---
    print(f"\nStep 5: Write CI-derived fields")
    try:
        write_derived_fields(entry_path, entry, derived, pr_author)
    except Exception as e:
        print(f"::error::Failed to write derived fields: {e}")
        print(f"❌ Write-back FAILED: {e}")
        sys.exit(1)

    print()
    print("✅ Quick check PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    main()
