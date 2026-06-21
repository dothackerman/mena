# UAT Protocol

Status: reusable installed-product UAT protocol

Use this protocol with `SCENARIOS.md` for Mena release-candidate UAT. The protocol
defines how testers execute scenarios, assign verdicts, preserve evidence, and
link failed runs to GitHub issues. It does not replace product requirements,
architecture decisions, or raw tester workspaces.

## Source Of Truth

Product and architecture behavior is defined in:

- `docs/requirements/poc-requirements.md`
- `docs/requirements/poc-installed-product-setup-use-case.md`
- `docs/requirements/poc-model-config-local.md`
- `docs/decisions/PDR-002-include-both-online-and-in-person-modes-in-poc-cli-scope.md`
- `docs/decisions/PDR-003-record-time-language-is-session-source-of-truth.md`
- `docs/decisions/ADR-024-linux-poc-capture-pipewire-first-with-dedicated-null-sink.md`
- `docs/decisions/ADR-028-installation-profiles-local-setup-state-and-preflight-separation.md`
- `docs/decisions/ADR-029-accepted-runtime-profile-state-revalidation-and-provenance.md`
- `docs/decisions/ADR-030-local-runtime-resource-stewardship-for-post-meeting-processing.md`
- `docs/decisions/ADR-031-runtime-fallback-authorization-and-setup-state.md`
- `docs/decisions/ADR-032-continuous-capture-with-rotating-source-segments.md`
- `docs/testing/live-pipewire/SCENARIOS.md`

## Evidence Boundaries

Keep committed UAT docs small and privacy-preserving.

Commit to the repository:

- reusable scenario definitions
- reusable protocol rules
- release/run summary files with scenario verdicts
- links or paths to tester evidence roots
- links to bug issues for failed scenarios

Do not commit:

- raw audio or transcripts
- raw session bundles
- raw PipeWire graph dumps
- screenshots or browser URLs
- provider tokens, private meeting names, notification text, or content labels
- large command logs or virtual environments

## Verdict Vocabulary

- `pass`: scenario met pass criteria with usable evidence.
- `pass_with_findings`: user path completed, but product quality or UX findings
  need follow-up.
- `blocked`: scenario could not reach the behavior under test because of a
  product, setup, environment, or release blocker.
- `fail`: scenario ran and violated pass criteria.
- `not_run`: intentionally deferred.
- `out_of_scope`: not part of the run.

## Run File Template

Create one run summary under `docs/testing/uat/runs/` for each formal UAT pass.

```text
# <release/run title>

Date:
Tester:
Release:
GitHub issue:
Evidence root:

## Scenario Verdicts

| Scenario | Verdict | Notes |
| --- | --- | --- |

## Failed Or Blocked Scenarios

### <Scenario ID>

Expected:
Observed:
Evidence:
Bug:
```

## Execution Order

Run scenarios in this order unless a blocker forces a narrower diagnostic run:

1. Release artifact identity and setup readiness.
2. Live environment readiness.
3. Live in-person and online recording smoke.
4. Runtime fallback/stewardship checks after setup readiness is complete.
5. Controlled known-fact processing fixtures.
6. End-to-end baselines for both PoC profiles.
7. Failure/resume checks.
8. Output quality review.
9. Stress/duration gate.
10. Compare-notes and issue triage.

## Failure Triage

When a scenario is `fail` or `blocked`:

- keep the scenario verdict in the run file
- create a slim bug issue when product behavior failed or needs implementation
  follow-up
- include observed result, expected result, release, scenario ID, and evidence
  pointers
- leave root-cause analysis and implementation design to the implementation or
  architecture follow-up
- do not paste raw logs or private evidence into GitHub

## Issue Linking

Failed run summaries should link to the bug issue once created. Bug issues should
link back to:

- the scenario ID in `SCENARIOS.md`
- the run summary file
- the tester evidence root or finding file

Use GitHub issues as execution containers, not as the durable source of product
truth.
