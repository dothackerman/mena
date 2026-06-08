# Requirements Engineering: BABOK + Phases Reference

## Two-Dimensional Matrix

BABOK v3 requirement types (vertical) cross the four requirements engineering phases (horizontal).

| | Elicitation | Analysis | Specification | Commitment |
|---|---|---|---|---|
| **Business** | Raw vision, goals | Validated objectives | Business requirements doc | Epic-level approval |
| **Stakeholder** | User pain points | Prioritized needs | Stakeholder reqs doc | Epic acceptance criteria |
| **Solution** | Feature ideas | Feasibility checked | Functional/non-functional spec | User stories, implementation issues |

## Key Rules

1. **Both dimensions are mandatory.** A requirement doesn't exist at a single point; it exists in a cell.
2. **You cannot skip phases.** Business requirement must go through all four phases.
3. **You can extract slices.** A solution requirement can be committed while its parent business requirement is still in elicitation.
4. **Traceability links required.** When extracting a slice, document the parent requirement explicitly.

## Mena Examples

| Concept | BABOK Type | RE Phase | Location |
|---------|-----------|----------|----------|
| Hardware-aware profiles | Business | Elicitation | `draft-hardware-aware-model-profiles.md` |
| Installed product setup readiness | Stakeholder | Commitment | `poc-installed-product-setup-use-case.md` |
| Local model config for PoC | Solution | Commitment | `poc-model-config-local.md` + Epic #16 |

## Anti-Patterns

- ❌ Posting raw elicitation to implementation issues
- ❌ Committing requirement without analysis phase
- ❌ Losing traceability between parent and sliced requirements
- ❌ Treating "solution requirement in elicitation" as actionable by developers

## Template Usage

For new requirements, copy `requirement-template.md` and set frontmatter:
```yaml
status: elicitation
phase: elicitation
hierarchy: business-requirement  # or stakeholder, solution
babok-type: business              # or stakeholder, solution
```

Phase changes documented in commit messages, not just file edits.
