# 003 — Treat qualification as untrusted, metered input/output

Status: accepted.

The model returns six strict schema fields. Trusted intake identity, endpoints, recipients and slots
never come from the model. System instructions and serialized untrusted data are separate. This
limits capabilities and rejects unsafe fields; it does not prove semantic injection immunity.

Each physical HTTP attempt, including retries/repair, needs a committed reservation. Explicit
operator rates and conservative input/output bounds produce a configured-rate estimated ceiling.
Ambiguous failures remain charged. Actual usage may be unknown; estimates are not invoices and
provider account caps remain necessary. Live mode fails closed without approval/rates/credentials.

Versioned prompts/schema hashes bind synthetic replay fixtures to product logic. Separate human
annotations define expected categories, budgets, timelines and routing. Replay runs through the real
qualification HTTP boundary, rather than comparing two fixtures. Its scores measure regression
behavior only. Live generation and an optional blinded rubric judge have explicit durable call/cost
limits and require separate approval; subjective quality stays unmeasured until actually evaluated.

Low-information/low-score valid qualifications proceed under the same structural policy; no hidden
score-to-action threshold is invented. Invalid output repairs once, then manual review with warnings.
