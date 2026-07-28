---
name: story-writer
description: Write user stories for the product backlog following the internal INVEST-based template — a user-voice story statement plus testable acceptance criteria. Use when asked to turn a feature request, requirement, or capability description into one or more well-formed agile user stories.
---

# User Story Writer (internal template)

Turn a feature request into well-formed user stories that follow the team's template.

## Output template (use exactly this shape)
For each story:

```
**Title:** <short imperative title>

**Story:** As a <specific role>, I want <capability> so that <benefit/why>.

**Acceptance Criteria:**
- Given <context>, when <action>, then <observable outcome>.
- ... (2-5 criteria, each independently testable)

**Notes:** <edge cases, non-goals, or open questions — optional>
```

## Quality bar (INVEST)
- **Independent** — the story stands alone; don't chain it to another story's completion.
- **Negotiable** — describe the need, not a rigid implementation.
- **Valuable** — the "so that" states real user/business value, not a restatement of the action.
- **Estimable** — scoped tightly enough to size; split epics into multiple stories.
- **Small** — one coherent capability per story.
- **Testable** — every acceptance criterion is a concrete, checkable Given/When/Then.

## Rules
- Use a **specific** role (e.g. "returning customer", "fraud analyst"), never "user" alone.
- Acceptance criteria must be observable outcomes, not internal steps.
- If the request is an epic, split it into several small stories rather than one large one.
- Do not invent product areas or requirements that were not stated or clearly implied.
