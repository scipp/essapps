# The framework from three sides

Three notebooks, one per kind of author, on one running example: `toy_sans.py`, a toy SANS reduction with a single-run spec, a multi-run spec of tables of its sample and can runs, a spec that reads the curve of the first, and two specs that compute a beam centre and a transmission, which the reduction takes typed in or by reference.

| Notebook | For | Shows |
|---|---|---|
| [workflow-authors.ipynb](workflow-authors.ipynb) | authors of workflow packages, such as esssans | the specs with their parameters, each written inline or given by reference as its type allows, the bindings, and a driver: what the framework gives a binding, and what its author promises |
| [client-authors.ipynb](client-authors.ipynb) | authors of notebooks, apps, and programs | the client API: what client code relies on, and what it owes |
| [framework-promises.ipynb](framework-promises.ipynb) | authors of the framework | the promises of the other two seen at work, and what keeping each costs in the user's process and on the service |

They run in this folder, with the packages installed as `pip install -e packages/essspec -e 'packages/essapps[test]'`.

[spec-form.ipynb](spec-form.ipynb) shows the app of `ess.apps.panel` on a client: a form generated from each spec's pydantic models. It needs Panel, which the extra `[test]` installs through `[panel]`, and Jupyter to show the app.
