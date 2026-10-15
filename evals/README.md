# Qualification replay regression suite

`cases.json` contains 14 independently annotated synthetic inquiries. `responses.json` contains
separate hand-authored raw completion fixtures, including malformed/unsafe responses. No model
was called to create these responses. Categories map directly to automation/dotnet/rag/ai_agent/other.
Valid low-information E09 follows normal qualification; score alone never authorizes extra actions.

```sh
python -m evals.run --offline --output artifacts/evals.json
```

The runner injects responses only at HTTP transport, then calls the actual intake/qualification
API with durable accounting. It reports initial schema validity (designed-invalid examples are
included), final routing, per-field correctness, structural safety, physical call count and content
hashes. Budget/currency/category/timeline must match; scores fall in annotated ranges; reasons
contain input-supported concepts. Every case currently must pass every applicable field and routing
check, stricter than an aggregate-only 95% threshold. All required safety/repair/manual cases must
pass. Missing cases, duplicate fixtures and stale provenance fail. Prompt hashes cover exact UTF-8
file bytes; schema hashes cover `json.dumps(model_json_schema(), sort_keys=True)` encoded as UTF-8.
Fixture hashes are never refreshed during an evaluation.

These results prove synthetic replay regression behavior, **not live model quality, general language
understanding or semantic injection resistance**. The production demo's fixed synthetic response is
separate from this diverse corpus. Optional live results remain unverified until explicitly run.

## Opt-in live generation and judge

No live calls or API credentials are needed for the default suite. After separate owner approval,
the operator supplies an environment key, model, positive input/output USD-per-million rates,
`LLM_RATE_SOURCE` and `LLM_SPEND_APPROVED=true`. Never put them in arguments or source. The example
ceilings below are placeholders to choose explicitly, not recommended spending approval:

```sh
python -m evals.live --approve-spend --provider anthropic --model APPROVED_MODEL \
  --max-calls APPROVED_CALL_COUNT --max-cost APPROVED_USD \
  --dataset evals/cases.json --ledger PRIVATE_LEDGER.db --output PRIVATE_RESULTS.json
python -m evals.judge --approve-spend --provider anthropic --model APPROVED_JUDGE \
  --max-calls APPROVED_CALL_COUNT --max-cost APPROVED_USD \
  --results PRIVATE_RESULTS.json --ledger PRIVATE_JUDGE_LEDGER.db --output PRIVATE_JUDGMENTS.json
```

Commands evaluate all supplied cases by default; they never silently truncate to the call limit.
A complete corpus may exceed a small approved call budget, which fails rather than claiming success.
For an explicitly approved subset, repeat `--case-id E01 --case-id E02` (or provide a separate dataset).
Subset reports identify the source/required counts and use `PASS_PARTIAL`, never a full-suite pass.
Missing requested IDs, empty input, unsuccessful generation/admission/provider calls, missing judge
qualifications or rubric failures produce `FAIL` and exit nonzero while retaining private per-case
reports. Aggregate required/result/evaluated/passed/failed/missing counts are included. Generation
success indicates structurally valid output only; subjective quality still needs separate assessment.

Each invocation uses one persistent run ledger, caps physical calls at 1–12 including retries/repair,
and commits conservative reservations before I/O. Exhaustion stops further I/O; ambiguous attempts
consume reservations. Results/ledgers default to operator-supplied private paths. Do not commit them.
Repeated CLI invocations are separately approved budgets, not an account-wide spend cap.

Judge rubric v1 scores reason relevance, grounding and uncertainty calibration 0–2, with short
inquiry evidence and an invented-budget/identity flag. Passing requires each score ≥1, mean ≥1.7,
and no invented budget/identity. The judge sees inquiry/reason, not expected score or candidate
provider. Strict untrusted judge JSON never controls production routing. Human spot-check all
failures and at least three passes. No synthetic judge replay is advertised as a live judge run.
