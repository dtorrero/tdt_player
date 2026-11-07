# 30 — Quality gates

A task is not "done" until these pass. Run them yourself, don't ask the human
to run them.

## Before finishing any code task
1. `make lint` — ruff check + ruff format --check (Python), syntax checks (shell).
2. `make test` — pytest (Python). Must be green.
3. If you added/changed behavior, add or update a test.
4. Update `CHANGELOG.md` under `## [Unreleased]` for user-visible changes.

## When a check fails
- Fix the root cause; don't disable rules or skip tests to go green.
- If a rule is genuinely wrong for this project, explain why and ask before
  editing config to ignore it.

## Definition of done (checklist)
- [ ] Branch used (not main)
- [ ] Lint passes
- [ ] Tests pass
- [ ] Commit message follows Conventional Commits
- [ ] No secrets / ignored files staged
- [ ] Changelog updated if user-visible
