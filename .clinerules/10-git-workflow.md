# 10 — Git workflow

## Branching
- NEVER commit or push directly to `main` or `master`.
- Create a working branch for every task:
  - `feat/<short-slug>`, `fix/<short-slug>`, `chore/<short-slug>`, `docs/<short-slug>`
- Kanban runs tasks in `git worktree`s — keep worktree-friendly workflows in mind.

## Commits
- Use Conventional Commits:
  - `feat: add download retry handling`
  - `fix: correct release-year parsing`
  - `docs:`, `chore:`, `refactor:`, `test:`, `perf:`
- Imperative mood, lowercase after the colon, no trailing period in subject.
- Keep the subject <= 72 chars; put detail in the body.

## Safety
- Do NOT `git push --force` to shared branches.
- Do NOT rewrite pushed history.
- Do NOT delete tracked files without explicit confirmation.
- Do NOT `git add` secret/ignored files.
- Stage files explicitly (`git add <path>`), avoid blind `git add -A` on mixed changes.

## PRs
- When asked to "ship", prefer opening a PR over merging to main.
