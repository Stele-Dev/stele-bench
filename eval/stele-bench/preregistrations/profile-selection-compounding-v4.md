# Profile-selection compounding experiment v4

Status: locked after the discarded v3 admission and before any cohort pair.
This version inherits `profile-selection-compounding-v3.md` in full except for
the outer cohort scheduling and funding fence below.

## Evidence carried forward

The discarded v3 admission, `profile-selection-admission-003`, passed every
inherited admission criterion. It validated the unchanged inner two-session
chain, treatment graph boundary, source reset, model pin, response grader, Rust
verifier, Gateway billing receipt, and prebuilt Claude/Rust task image. No v3
admission observation contributes to the cohort.

## Frozen cohort schedule

The cohort remains exactly 10 paired chains with the inherited analysis and
stopping rules. It is executed as ten batches of one pair:

1. attempts are permanently numbered 1 through 10 before attempt 1 starts;
2. odd attempts run control then treatment, even attempts treatment then control;
3. only the next attempt index may run;
4. a batch that creates a receipt consumes its index even when invalid or
   interrupted, and is never replaced;
5. a failure before any batch receipt exists does not consume the index;
6. every batch has the inherited four-session `$40.00` worst-case fence;
7. batch receipts are immutable once their digest enters the cohort ledger;
8. the ledger exposes only operational validity and settled Gateway cost until
   all 10 attempts exist; treatment-effect aggregation happens only at N=10.

After each batch, continuation may be checked only for infrastructure validity,
settled billing, and available quota. No prompt, graph, model, task, validator,
limit, arm, metric, or analysis change may be made from an intermediate result.
If execution stops early for safety or funding, the cohort is incomplete and no
benchmark claim is made from it.

## What changes

Only the outer oracle changes. A durable resumable cohort ledger freezes the 10
identities and arm order, invokes the already-admitted one-pair chain, applies
the money fence per batch, rejects skips and duplicates, and aggregates the
unchanged attempt receipts after attempt 10. No new paid admission is required
because the admitted inner chain is unchanged; resume and aggregation are
validated with zero-model tests before cohort attempt 1.
