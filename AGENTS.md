# trace2rules

## Architecture

Keep `src/trace2rules/cli.py` limited to Typer option parsing and wiring; put shared helpers in `src/trace2rules/cli_common.py` and command behavior in `src/trace2rules/commands/audit.py`, `src/trace2rules/commands/report.py`, or `src/trace2rules/commands/holdout.py`.

`src/trace2rules/ingest/` adapters produce canonical events; `mining/` emits findings; `synthesis/` formats prompts and selection. Change the owning package module and its `tests/test_<module>.py` instead of editing every stage.

## Dependencies

Update dependencies only in `pyproject.toml` and commit the regenerated `uv.lock`; use `uv sync --frozen` for environment setup and validation.

## Validation

For a behavior change under `src/trace2rules/`, reproduce the defect with a failing `uv run pytest -q` before editing source; if no existing test covers it, add a failing test first.

Narrow one failing CLI case with:

```bash
uv run pytest -q tests/test_cli.py -k <pattern>
```
