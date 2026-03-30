# CUBE Registry

The official catalog of CUBE-compliant benchmarks.

[**Browse benchmarks →**](https://the-ai-alliance.github.io/cube-registry)

> **Note:** The live site above requires GitHub Pages to be enabled for this repository
> (Settings → Pages → Source: Deploy from branch `main`, folder `/docs`).
> Until then, the generated HTML is available in the [`docs/`](docs/index.html) folder.

---

## What is this?

The CUBE Registry is a community-maintained index of benchmarks that implement the
[CUBE standard](https://github.com/The-AI-Alliance/cube-standard). Any CUBE-compliant
evaluation platform or training harness can discover and run registered benchmarks without
custom integration.

Each benchmark is a single YAML file in `entries/`. The registry does not host benchmark
code or data — it points to PyPI packages that do.

---

## Submitting a benchmark

### Prerequisites

Your benchmark package must:
- Be published on PyPI
- Implement the CUBE `Benchmark` and `Task` interfaces
- Expose at least one debug task via `cube/debug_tasks`

The easiest way to create a compliant package is with the CUBE skill in
[Claude Code](https://claude.ai/claude-code):

```
/create-cube
```

### Submission steps

1. Fork this repository
2. Create `entries/<your-benchmark-id>.yaml` (see template below)
3. Open a pull request

CI will validate your entry and auto-merge if it passes. No human review needed.

### Entry template

```yaml
id: your-benchmark-id          # must match filename, globally unique
name: Your Benchmark Name
version: "1.0.0"               # must match PyPI version exactly
description: >
  One paragraph describing what your benchmark tests and why it matters.
package: your-pypi-package-name

authors:
  - github: your-github-handle
    name: Your Name

legal:
  wrapper_license: MIT          # license of this cube wrapper code
  benchmark_license:
    reported: "CC-BY-4.0"      # SPDX identifier, as you understand it
    source_url: "https://github.com/you/benchmark/blob/main/LICENSE"
  notices: []                   # see spec for notice types

paper: "https://arxiv.org/abs/..."   # optional
tags: [web, coding, os, gui, mobile, science, math, multi-agent]
getting_started_url: "https://..."

supported_infra: [aws]          # providers to run compliance checks on
max_concurrent_tasks: 1
parallelization_mode: sequential  # sequential | task-parallel | benchmark-parallel
```

Fields populated automatically by CI (do not fill):
`status`, `resources`, `task_count`, `has_debug_task`, `has_debug_agent`,
`action_space`, `features`, `stress_results_url`

### Updating your entry

Open a PR modifying your existing YAML. CI verifies you are a registered author
(via `OWNERS.yaml`) and auto-merges if checks pass.

---

## Compliance checks

Every submission goes through two tiers:

| Tier | When | What | Cost |
|---|---|---|---|
| Quick check | On PR (~2 min) | Schema, PyPI install, API introspection | Free |
| Slow check | Post-merge (async) | Full debug episode on real infra | ~$0.05/VM cube |

A slow check failure opens a GitHub issue tagging the entry authors.
Entries remain in the registry regardless — platforms decide which tier they require.

---

## Legal

License information in this registry is **self-reported by cube developers** and has not been
verified by the AI Alliance. Always consult the `benchmark_license.source_url` and the
original benchmark authors for authoritative terms.

By submitting an entry, contributors attest that license information is accurate to the best
of their knowledge. See [CONTRIBUTOR_AGREEMENT.md](CONTRIBUTOR_AGREEMENT.md) for full terms.

---

## Specification

Full registry design: [cube-standard/design/registry_specs.md](https://github.com/The-AI-Alliance/cube-standard/blob/design/registry-specs/design/registry_specs.md)
