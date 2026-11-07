# 40 — Safety (read this before any destructive action)

The human may be approving from a phone; a bad approval is costly. Default to
"No" and ask.

## Never without explicit confirmation
- `rm -rf` anything outside the repo, or on tracked files inside it.
- `sudo`, installing/changing system packages, editing `/etc`, firewall, systemd.
- `git push --force`, history rewrite, deleting branches/remotes.
- `git reset --hard`, `git clean -fdx` (destroys uncommitted work).
- Network installs (`pip/npm install`) into system envs — prefer project venvs.
- Anything touching the Tailscale/Kanban security posture (binding to 0.0.0.0,
  exposing ports, tunnels).

## Secrets — never read, print, echo, or commit
- `*.transmission-credentials.json`, `.env`, `.env.*`, `*.pem`, `*.key`,
  `id_rsa`, `id_ed25519`, anything under `**/secrets/**`.
- Do not cat/grep/diff these. If a task seems to need a secret, ask where the
  value should come from (env var) instead of reading the file.

## Scope discipline
- Only modify files needed for the task. No drive-by refactors.
- Do not touch `.git/`, `.clinerules/`, or CI config unless the task is about them.

## Reporting
- After risky steps, state exactly what changed and how to undo it.
