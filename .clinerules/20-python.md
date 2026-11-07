# 20 — Python conventions

Target: **Python 3.14** (match the `python3 --version` of the machine).

## Style
- Format and lint with `ruff` (see `pyproject.toml`). Run via `make lint` / `make format`.
- Import order: stdlib, third-party, local — `ruff` enforces `isort`-style.
- Prefer f-strings; prefer `pathlib` over `os.path`.
- Add type hints to public functions and non-trivial logic.
- Small functions, meaningful names, docstrings on public symbols.

## Layout (matches existing projects)
- Library/app code in its own package dir; tests in `tests/`.
- Distribution packaging (if any) under `packaging/`.
- Config in `pyproject.toml` (no `setup.py` unless legacy).

## Tests
- `pytest` in `tests/`, one file per module, `test_*.py`.
- Keep a fast smoke test so the suite always has something green.

## Quality gates
- Before declaring a task done: `make lint && make test` must pass (see 30).
