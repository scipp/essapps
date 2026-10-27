## Project Overview

ESSapps is the application framework for the European Spallation Source (ESS).

## Development Commands

### Environment Setup

In the devcontainer, micromamba is auto-activated -- `python`, `pytest`, `tox` etc. just work.

**Worktree Setup** (when launched with `claude -w`):

```sh
python -m venv .venv && .venv/bin/pip install -e ".[test]"
```

Shell state does not persist between Bash calls, so the venv is never active. Run
tools from it explicitly, e.g. `.venv/bin/python -m pytest`. Plain `python`/`pytest`
resolve to the conda env, which imports the main checkout.

## Code Style

- Formatting: `ruff` (88 char lines)
- Type hints: required; `mypy` used but not strictly enforced
- Docstrings: NumPy format, no type annotations in params (sphinx-autodoc-typehints handles it)

```python
def process(x: int) -> float:
    """Short description.

    Parameters
    ----------
    x:
        Description of x.

    Returns
    -------
    :
        Description of return value.
    """
```

Single-sentence: `"""Returns the number of dimensions."""`
