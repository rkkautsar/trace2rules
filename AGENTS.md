# trace2rules

## Architecture

`src/trace2rules/ingest/` adapters and service produce canonical events; `mining/` emits findings and sequences; `synthesis/` formats report prompts and optional rules; `cli.py` wires commands. Change the owning package module and its `tests/test_<module>.py` instead of editing every stage.

## Reading code

Locate the symbol with `rg -n '^(def|class) ' src/trace2rules/<package>/<module>.py`, then read only the enclosing range. Avoid whole-file `cat -n` or `nl -ba` on `synthesis/service.py`, `mining/core.py`, `mining/sequences.py`, `cli.py`, and `ingest/normalize.py`.

## Validation

After edits, run:

```bash
uv run pytest -q
```

Narrow one failing CLI case with:

```bash
uv run pytest -q tests/test_cli.py -k <pattern>
```
