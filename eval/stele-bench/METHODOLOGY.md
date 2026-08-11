# Methodology

## Evaluation question

Stele Bench measures whether persistent project memory changes correctness or
the work a coding agent performs under a controlled, paired task. It does not
measure whether the underlying model is generally smarter and it does not turn
different tasks into one productivity percentage.

Each pair fixes the source commit, prompt, challenge, hidden verifier, model,
harness, turn and cost limits, container image, graph fixture, and stopping
rule. The principal comparison is Stele disabled versus a frozen Stele graph.
Some cohorts isolate a narrower mechanism, such as file-boundary reminders with
prompt-time retrieval disabled.

## Question admission

A task must be unambiguous and its intended correct answer must actually depend
on the memory under test. Merely making an answer obscure is insufficient. The
control may inspect the real source normally, including comments and tests.

Behavior and workspace outcomes are graded where possible. Vocabulary markers
are used only when the required concept has a stable, unambiguous expression.
Deterministic hidden verifiers are preferred.

Before paid execution, the protocol freezes:

- task prompt and allowed output paths;
- source repository and full commit;
- challenge and verifier bytes and SHA-256 digests;
- graph fixture and treatment condition;
- model, harness, image, resources, turn cap, and cost cap;
- sample size, stopping rule, validity floor, and analysis.

Changing any of these after observing a run creates a new preregistered version.
The earlier version remains in the record.

## Execution and isolation

The host-side oracle materializes the pinned source commit from Git objects into
a Git-less project tree. Auto-loaded project instructions and project bindings
declared by the certification policy are removed identically from every arm and
recorded in a redaction receipt. Static certification checks every agent-visible
surface for benchmark awareness and solution leakage before state is created or
model tokens are spent.

Harbor runs each arm in a fresh container. The no-Stele arm receives no graph
credentials or integration artifact. A graph arm receives a fresh principal,
an exact restored graph, a project binding, and the pinned Stele executable.
The provisioner snapshots the graph independently after execution and destroys
attempt state in a finalizer.

## Scoring and mechanism evidence

Correctness and efficiency are separate outcomes. Efficiency includes tool
calls, agent execution time, wall time, input/output tokens, and cost evidence.
Pair identity is retained. Larger analyses will operate on treatment-control
deltas rather than flattening attempts as independent observations.

Trajectories are inspected to distinguish three claims:

1. memory was delivered;
2. the agent observably used it;
3. the scored outcome changed.

One does not imply the next. A correct treatment arm with a delivered memory is
not evidence that memory caused success when the control also succeeds.

## Admission and negative results

Execution state such as `completed` is not scientific admission. Every
published run receives a separate `accepted` or `excluded` decision with a
reason code and explanation.

Negative and null results are published. Excluded outcomes are also retained,
but are never used in an effect estimate. Cleanup may reorganize or redact host
paths and credentials; it may not remove a run because its direction is
unfavorable.

## Reproducibility artifacts

For accepted runs, the public bundle includes the normalized result, attempt
receipts, certification receipt, Harbor configuration and result, verifier
evidence, mutation audit, logs, and redacted ATIF trajectory when retained. A
manifest records the SHA-256 digest and byte length of every exported file.

Full trajectories are valuable evidence, not a universal admission requirement.
When a mechanism claim depends on a trajectory, the trajectory or an equivalent
complete trace must be published. Otherwise the report must narrow or remove
the mechanism claim.

## Reporting boundaries

The August 2026 report contains two N=1 cohorts and one N=2 cohort. It reports
raw pairs and descriptive differences. It makes no general Stele productivity,
win-rate, statistical-significance, or stable average cost claim.

Repeated trials and task diversity answer different questions. Repeats estimate
reliability on one frozen task. Different tasks estimate generalizability. A
future multi-task analysis should preserve task and task-family hierarchy when
bootstrapping intervals.

## External guidance

- OpenAI, [A shared playbook for trustworthy third party evaluations](https://openai.com/index/trustworthy-third-party-evaluations-foundations/), 2026-05-29.
- Anthropic, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), 2026-01-09.
- Anthropic, [Quantifying infrastructure noise in agentic coding evals](https://www.anthropic.com/engineering/infrastructure-noise), 2026-02-05.
- [SWE-bench experiments](https://github.com/swe-bench/experiments), used as a precedent for retaining predictions, metadata, logs, and trajectories.
- METR, [Details about METR's preliminary evaluation of Claude 3.7](https://evaluations.metr.org/claude-3-7-report/), used as a precedent for explicit preliminary labels and hierarchical analysis.
