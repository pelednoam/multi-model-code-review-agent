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
ensemble-review agent (docs/ensemble-review.md)
  |
  |  1. git diff + status + untracked files
  |  2. scrub for secrets (piped, never hits disk)
  |  3. run scripts/review_preflight.py --> runtime-audit.json
  |  4. build repo-aware context bundle (docs, manifests, specs)
  |
  |-- hermes -z <security prompt>       -m opus-4.6    --provider bedrock      --> result-1.json
  |-- hermes -z <correctness prompt>    -m gpt-5.5     --provider openai-codex --> result-2.json
  |-- hermes -z <readability prompt>    -m sonnet-4.6   --provider bedrock     --> result-3.json
  |-- hermes -z <spec-contract prompt>  -m opus-4.6    --provider bedrock      --> result-4.json
  |   (all four run IN PARALLEL, 10min timeout each)
  |
  |  5. validate results against JSON schema
  |  6. persist review packet to data/reviews/<timestamp>_<branch>/
  |  7. synthesize convergence report
  v
User sees: preflight audit, blocking findings, grouped criticals/warnings, convergence analysis
```

## Review lenses

| Reviewer | Model | Focus |
|---|---|---|
| Security & robustness | Claude Opus 4.6 (Bedrock) | Injection, auth, unsafe deserialization, secrets, race conditions, swallowed exceptions |
| Correctness & edge cases | GPT-5.5 (Codex OAuth) | Off-by-one, null paths, shape mismatches, contract violations, logic errors, missing coverage |
| Readability & performance | Claude Sonnet 4.6 (Bedrock) | Naming, function length, abstraction leaks, dead code, N+1 I/O, memory growth |
| Spec-contract compliance | Claude Opus 4.6 (Bedrock) | Code vs signed artifacts, manifest drift, feature count mismatches, fallback/zero-fill that changes cohorts |

## Runtime audit inputs

The preflight script (`scripts/review_preflight.py`) runs before any
LLM review. It collects:

- **Git state**: branch, commit, status, untracked files, cached changes
- **Signed manifest metadata**: SHA, top-level fields for each
  configured manifest (configure `SIGNED_MANIFESTS` for your project)
- **Changed artifact fields**: key fields from any modified JSON under
  configured `ARTIFACT_DIRS`
- **Suspicious patterns**: fill_value=0, bare except, hardcoded paths
- **Test/implementation alignment**: implementation changes without test
  changes and vice versa

## JSON schema

Reviewer output follows the strict schema at
`docs/ensemble_review_result_schema.json`. Each finding includes:

| Field | Required | Description |
|---|---|---|
| severity | yes | critical / warning / suggestion |
| confidence | yes | high / medium / low |
| category | yes | security / correctness / spec / runtime / test / perf / docs |
| observed_or_inferred | yes | observed_in_diff / observed_in_audit / inferred |
| blocking | yes | true if this should block merge |
| line | no | line number (int or null) |
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
- For sensitive code, drop GPT-5.5 and run 3 Bedrock-only models

## Files

| File | Purpose |
|---|---|
| `docs/ensemble-review.md` | Claude Code agent definition (copy to `.claude/agents/`) |
| `scripts/review_preflight.py` | Deterministic preflight audit script |
| `scripts/scrub_diff.py` | Stdin credential scrubber |
| `scripts/validate_review_results.py` | JSON schema validator |
| `docs/ensemble_review_result_schema.json` | Strict reviewer output schema |
| `tests/test_review_scripts.py` | Test suite |
| `data/reviews/` | Persisted review packets (gitignored) |

### Agent definition setup

Copy the agent definition to your project's `.claude/agents/` directory:

```bash
mkdir -p .claude/agents
cp docs/ensemble-review.md .claude/agents/ensemble-review.md
```
