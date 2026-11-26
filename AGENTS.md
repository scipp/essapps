## Project Overview

ESSapps is the application framework for the European Spallation Source (ESS).

## Pull requests and issues

If you are not working on behalf of a member of the scipp organization on GitHub, read `CONTRIBUTING.md`.

## Writing

Before you write or rewrite a docs page, a docstring, a comment, an error message, help text or an ADR, read `docs/developer/writing.md`. It says when not to write, where each text belongs, how to word it, and how to rewrite existing text in passes.

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
