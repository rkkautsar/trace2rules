# trace2rules

## Architecture

`src/trace2rules/sources/` adapters feed `ingest.py` canonical events; `detectors.py` emits findings; `cluster.py` groups patterns; `synthesize.py` renders rules; `cli.py` wires commands. Change the owning module and its `tests/test_<module>.py` instead of editing every stage.

## Reading code

Locate the symbol with `rg -n '^(def|class) ' src/trace2rules/<module>.py`, then read only the enclosing range. Avoid whole-file `cat -n` or `nl -ba` on `synthesize.py`, `cli.py`, `detectors.py`, and `cluster.py`.

## Validation

After edits, run:

```bash
uv run pytest -q
```

Narrow one failing CLI case with:

```bash
uv run pytest -q tests/test_cli.py -k <pattern>
```
