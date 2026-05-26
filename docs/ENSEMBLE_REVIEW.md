# Multi-Model Ensemble Code Review

A review-only pipeline that runs four frontier LLMs in parallel through
Hermes Agent, each with a different review lens, preceded by a
deterministic preflight audit. Synthesizes findings with per-reviewer
attribution and evidence-based convergence scoring.

## Why

Static diff review is insufficient for artifact-driven research code.
A single-model reviewer can miss contract violations between code and
signed manifests (e.g. 782 features in code vs 662 in the column
manifest). This pipeline addresses three gaps:

1. **Model diversity**: four architecturally different models surface
   findings that any single reviewer would miss.
2. **Spec-contract lens**: a dedicated reviewer cross-references code
   against signed artifacts, manifests, and specs -- catching the class
   of failures that pure code review misses.
3. **Runtime audit**: a deterministic preflight script collects artifact
   metadata and manifest comparisons before any LLM runs, giving
   reviewers machine-verified evidence instead of relying on inference.

## Architecture

```
Claude Code (dev pipeline)
  |
  |  "run an ensemble review"
  v
ensemble-review agent (.claude/agents/ensemble-review.md)
  |
  |  1. git diff + status + untracked files
  |  2. scrub for secrets
  |  3. run scripts/review_preflight.py --> runtime-audit.json
  |  4. build repo-aware context bundle (docs, manifests, specs)
  |
  |-- hermes -z <security prompt>       -m opus-4.6    --provider bedrock      --> result-1.json
  |-- hermes -z <correctness prompt>    -m gpt-5.5     --provider openai-codex --> result-2.json
  |-- hermes -z <readability prompt>    -m sonnet-4.6   --provider bedrock     --> result-3.json
  |-- hermes -z <spec-contract prompt>  -m opus-4.6    --provider bedrock      --> result-4.json
  |   (all four run IN PARALLEL)
  |
  |  5. validate results against JSON schema
  |  6. persist review packet to data/reviews/<timestamp>_<branch>/
  |  7. synthesize convergence report
  v
User sees: preflight audit, blocking findings, grouped criticals/warnings, convergence analysis
```

## Prerequisites

1. **Hermes Agent** installed and in PATH (`hermes --version`)
2. **AWS Bedrock** authenticated (`hermes auth status bedrock`) with
   access to Claude Opus 4.6 and Sonnet 4.6
3. **OpenAI Codex OAuth** authenticated (`hermes auth status openai-codex`)
   for GPT-5.5
4. **Python venv** with project deps installed (`pip install -e ".[dev]"`)

## How to use

### From Claude Code

Say any of:
- "run an ensemble review"
- "use the ensemble-review agent on the current branch"
- "ensemble review these files: research/alphafold_backbone/data/*.py"

The agent will:
1. Collect the diff, git status, and untracked files
2. Scrub for credential patterns
3. Run the deterministic preflight audit
4. Build a repo-aware context bundle with relevant specs and manifests
5. Launch four parallel Hermes sessions
6. Validate and persist results
7. Synthesize a convergence report

## Review lenses

| Reviewer | Model | Focus |
|---|---|---|
| Security & robustness | Claude Opus 4.6 (Bedrock) | Injection, auth, unsafe deserialization, secrets, race conditions, swallowed exceptions, PHI/FDA-path scrutiny |
| Correctness & edge cases | GPT-5.5 (Codex OAuth) | Off-by-one, null paths, shape mismatches, contract violations, logic errors, missing coverage |
| Readability & performance | Claude Sonnet 4.6 (Bedrock) | Naming, function length, abstraction leaks, dead code, N+1 I/O, memory growth, GPU leaks |
| Spec-contract compliance | Claude Opus 4.6 (Bedrock) | Code vs signed artifacts, manifest drift, feature count mismatches, fallback/zero-fill that changes cohorts, tests that assert current vs signed behavior |

## Runtime audit inputs

The preflight script (`scripts/review_preflight.py`) runs before any
LLM review. It collects:

- **Git state**: branch, commit, status --short, untracked files, cached changes
- **Signed manifest metadata**: column counts (expected 662), split counts
  (train/dev/test per cell), SHA comparisons against signed artifacts
- **Changed artifact fields**: n_features, seeds, cell counts from any
  modified JSON under data/runs/, data/splits/, docs/proposals/
- **Suspicious patterns**: fill_value=0, bare except, hardcoded absolute
  paths, silent NaN fills
- **Test/implementation alignment**: implementation changes without test
  changes and vice versa

This audit JSON is given to every reviewer alongside the diff. Reviewers
are instructed to treat mismatches between runtime artifacts and signed
manifests as potential blocking findings.

## JSON schema

Reviewer output follows the strict schema at
`docs/review/ensemble_review_result_schema.json`. Each finding includes:

| Field | Required | Description |
|---|---|---|
| severity | yes | critical / warning / suggestion |
| confidence | yes | high / medium / low |
| category | yes | security / correctness / spec / runtime / test / perf / docs |
| observed_or_inferred | yes | observed_in_diff / observed_in_audit / inferred |
| blocking | yes | true if this should block merge |
| contract_reference | no | path to the spec/manifest this finding relates to |
| repro_command | no | command to reproduce or verify |

## Convergence scoring

Single-reviewer findings are NOT automatically low-signal:

> A single finding is HIGH PRIORITY if it has concrete evidence against
> a signed artifact, spec, or runtime output -- even if only one
> reviewer flags it.

Evidence-backed findings from the spec-contract reviewer are always
high-priority. Findings flagged by multiple reviewers are high-signal.
Inferred findings from one reviewer with no evidence are worth a glance
but lower priority.

## Review packet persistence

Review evidence is persisted to `data/reviews/<timestamp>_<branch>/`:

- `diff.patch` -- scrubbed diff
- `context.md` -- repo-aware context bundle
- `runtime-audit.json` -- preflight audit output
- `prompt-{1,2,3,4}.txt` -- reviewer prompts
- `result-{1,2,3,4}.json` -- raw reviewer output
- `report.md` -- synthesized convergence report

## Cost

Four frontier model API calls per review (2x Opus, 1x GPT-5.5, 1x
Sonnet). For a typical feature branch (200-1000 lines), expect ~$5-12
total. For a large branch (5000+ lines), expect ~$15-40.

## Privacy

- Bedrock calls stay within your AWS account boundary
- Codex OAuth calls go through ChatGPT's backend API
- For FDA-path code where cross-provider exposure is a concern, drop
  GPT-5.5 and run 3 Bedrock-only models (Opus security, Opus
  spec-contract, Sonnet readability+perf)

## Files

| File | Purpose | Tracked in git? |
|---|---|---|
| `docs/agents/ensemble-review.md` | Canonical agent definition (source of truth) | yes |
| `.claude/agents/ensemble-review.md` | Active agent loaded by Claude Code | no (`.claude/` is gitignored) |
| `scripts/review_preflight.py` | Deterministic preflight audit script | yes |
| `scripts/validate_review_results.py` | JSON schema validator for reviewer output | yes |
| `docs/review/ensemble_review_result_schema.json` | Strict JSON schema for reviewer output | yes |
| `data/reviews/` | Persisted review packets | no (gitignored) |

### Agent definition sync

`.claude/` is gitignored so the agent definition is not tracked. The
canonical copy lives at `docs/agents/ensemble-review.md` (tracked).
To sync after pulling:

```bash
mkdir -p .claude/agents
cp docs/agents/ensemble-review.md .claude/agents/ensemble-review.md
```

When editing the agent, edit `docs/agents/ensemble-review.md` and re-copy.
