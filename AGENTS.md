## Project Overview

ESSapps is the application framework for the European Spallation Source (ESS).

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
