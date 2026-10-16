# Multi-Model Ensemble Code Review

A review-only pipeline that runs four frontier LLMs in parallel -- local
CLIs (claude, codex, agy) by default, Hermes Agent on Bedrock when opted
in -- each with a different review lens, preceded by a deterministic
preflight audit. Synthesizes findings with per-reviewer
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
  |  "full review" (full mode; "review this" / "ensemble review" run quick mode)
  v
ensemble-review agent (docs/ensemble-review.md)
  |
  |  1. git diff + status + untracked files
  |  2. scrub for secrets (piped, never hits disk)
  |  3. run scripts/review_preflight.py --> runtime-audit.json
  |  4. build repo-aware context bundle (docs, manifests, specs)
  |
  |-- claude -p opus OR hermes opus                     --> result-1.json (security)
  |-- codex exec (GPT-5.5) OR claude -p haiku OR hermes haiku
  |                                                     --> result-2.json (correctness)
  |-- agy (Gemini, sandboxed) OR gemini (opt-in) OR claude -p sonnet OR hermes sonnet
  |                                                     --> result-3.json (readability)
  |-- claude -p opus OR hermes opus                     --> result-4.json (spec-contract)
  |   (pinned to Bedrock -- .use-hermes, --prefer-hermes, or a "hermes/bedrock/isolated
  |    review" request: hermes first in every slot where it is installed)
  |   (all four run IN PARALLEL; the loop's deadline scales with the diff, full mode's
  |    is REVIEWER_TIMEOUT, 600s by default, with the watchdog at +90s)
  |
  |  5. validate results against JSON schema
  |  6. persist review packet to data/reviews/<timestamp>_<branch>/
  |  7. synthesize convergence report
  v
User sees: preflight audit, blocking findings, grouped criticals/warnings, convergence analysis
```

## Review lenses

| Reviewer | Routing chain | Focus |
|---|---|---|
| Security & robustness | Opus via Claude CLI OR Hermes/Bedrock (first when pinned to Bedrock) | Injection, auth, unsafe deserialization, secrets, race conditions, swallowed exceptions |
| Correctness & edge cases | GPT-5.5 via Codex CLI (preferred) OR Haiku via Claude CLI OR Hermes/Bedrock; a project pinned to Bedrock runs Haiku via Hermes first, and OpenAI only with `REVIEW_USE_CODEX=1` | Off-by-one, null paths, shape mismatches, contract violations, logic errors, missing coverage |
| Readability & performance | agy -- Antigravity CLI, best Gemini model listed, under a verified bwrap sandbox (preferred) OR Gemini CLI (only with `REVIEW_USE_GEMINI=1`) OR Sonnet via Claude CLI/Hermes; a project pinned to Bedrock runs Sonnet via Hermes first, and Google only if asked | Naming, function length, abstraction leaks, dead code, N+1 I/O, memory growth |
| Spec-contract compliance | Opus via Claude CLI OR Hermes/Bedrock (first when pinned to Bedrock) | Code vs signed artifacts, manifest drift, feature count mismatches, fallback/zero-fill that changes cohorts |

## Runtime audit inputs

The preflight script (`scripts/review_preflight.py`) runs before any
LLM review. It collects:

- **Git state**: branch, commit, status, untracked files, cached changes
- **Signed manifest metadata**: SHA, top-level fields for each
  configured manifest (`signed_manifests` in `review-config.json` at the repo root)
- **Changed artifact fields**: key fields from any modified JSON under
  `artifact_dirs` (in `review-config.json`; `data/` and `docs/` by default)
- **Suspicious patterns**: fill_value=0, bare except, hardcoded paths
- **Test/implementation alignment**: implementation changes without test
  changes and vice versa
- **Coverage gaps**: per-file line + branch coverage for changed source
  files (target configurable via `COVERAGE_TARGET`, default 100%). The
  correctness reviewer turns each gap into a concrete test-case finding.

## Mandatory CI gate

The convergence loop (`scripts/review_until_converged.py`) and the agent's
merge-commit step both enforce a four-step gate. **All four must pass**:

1. `ruff check .`
2. `ruff format --check .`
3. `mypy scripts/` (or project target)
4. `pytest tests/ -x -q`

A missing tool causes a graceful skip with a note, not a failure -- but in
production all four should be installed. The gate output is persisted to
`gate-output.txt` per round.

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
- `result-{1,2,3,4}.json` -- each reviewer's review, extracted from its
  output and checked (a placeholder with `"reviewer": "unknown"` for a slot
  that produced none)
- `stdout-N.txt` / `claude-raw-N.json` / `codex-out-N.txt` -- each reviewer's
  raw answer (`backend-N.txt` says which CLI ran it)
- `stderr-N.txt`, `extract-result-N.err` -- why a slot produced no result
- `round.log` -- the runner's console output
- `report.md` -- synthesized convergence report

## Cost

With the local CLIs (the default), a review costs nothing beyond the
subscriptions they sign in with. On Hermes/Bedrock (opt-in), it is four
API calls (2x Opus, 1x Haiku, 1x Sonnet): for a typical feature branch
(200-1000 lines) expect ~$5-12, for a large one (5000+ lines) ~$15-40.

## Privacy

- The diff, the audit and the context bundle are scrubbed of
  credential-shaped lines first. A redaction in the diff stops the review;
  in the audit's quoted lines and in the context bundle the line is
  replaced and the review goes on
- agy, on by default wherever it is installed, sends them to Google;
  `REVIEW_USE_AGY=0` switches it off
- Codex calls go through ChatGPT's backend API, and codex can read your
  home directory
- Bedrock calls stay within your AWS account boundary; a project pinned
  to Bedrock (`.use-hermes`) sends nothing to OpenAI or Google unless
  asked by name

## Files

| File | Purpose |
|---|---|
| `docs/ensemble-review.md` | Claude Code agent definition (copy to `.claude/agents/`) |
| `scripts/review_preflight.py` | Deterministic preflight audit script |
| `scripts/scrub_diff.py` | Stdin credential scrubber |
| `scripts/validate_review_results.py` | JSON schema validator |
| `docs/ensemble_review_result_schema.json` | Strict reviewer output schema |
| `scripts/review_until_converged.py` | Convergence loop |
| `scripts/review_loop/` | The loop's package; also the full-mode runner's checks, extraction and agy's sandbox |
| `docs/agy-sandbox.md` | What agy's bubblewrap sandbox closes, what it does not, and its switches |
| `docs/codex-sandbox.md` | Why codex's sandbox is verified before use, and how to fix a host where it is not read-only |
| `tests/` | Test suite |
| `data/reviews/` | Persisted review packets (gitignored) |

### Agent definition setup

Copy the agent definition to your project's `.claude/agents/` directory:

```bash
mkdir -p .claude/agents
cp docs/ensemble-review.md .claude/agents/ensemble-review.md
```
