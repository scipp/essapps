# The framework from three sides

Three notebooks, one per kind of author, on one running example: `toy_sans.py`, a toy SANS reduction with a single-run spec, a multi-run spec, and a spec that reads a curve.

| Notebook | For | Shows |
|---|---|---|
| [workflow-authors.ipynb](workflow-authors.ipynb) | authors of workflow packages, such as esssans | the spec and the binding: what the framework gives a binding, and what its author promises |
| [client-authors.ipynb](client-authors.ipynb) | authors of notebooks, apps, and programs | the client API: what client code relies on, and what it owes |
| [framework-promises.ipynb](framework-promises.ipynb) | authors of the framework | the promises of the other two seen at work, and what keeping each costs in the user's process and on the service |

They run in this folder, with the packages installed as `pip install -e packages/essspec -e 'packages/essdispatch[test]'`.
