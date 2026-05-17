# Mena Schemas

This package contains schemas for persisted Mena contracts.

Name persisted schema files as:

```text
<contract-name>.v<major>.schema.json
```

Examples:

- `session-record.v1.schema.json`
- `artifact-manifest.v1.schema.json`
- `processing-state.v1.schema.json`
- `transcript.v1.schema.json`
- `diarization.v1.schema.json`

Schema versions are local to the contract they describe.
Do not introduce a global application schema version just to coordinate unrelated persisted formats.

During the PoC, each persisted contract starts in its `v1` epoch.
Before meaningful tester sessions exist, a PR may refine the still-forming `v1` shape directly if the PR states that explicitly.
After sessions exist that must be preserved, additive compatible fields may remain in `v1`; breaking changes require a new major schema.
