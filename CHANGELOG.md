# Changelog

All notable changes to **tdt_player** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) +
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Adopted the shared agent workflow from the `remoteCline` base:
  `.clinerules/`, `.clineignore`, `.editorconfig`, `.pre-commit-config.yaml`,
  a `Makefile`, ruff config in `pyproject.toml`, and git-ignore hygiene for
  secrets/caches/worktrees.
