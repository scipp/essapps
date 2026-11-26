# ADR 0012: A batch is a template and a table

- Status: accepted
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *label*, *header*, *source*, *definitions* are the words of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md).
*Template*, *blank* and *fill* are those of `ess.apps.Template`: a spec, some values, and blanks that each use fills.
*Row*, *key*, *cell*, *result* are those of [batch.md, "Terms"](../../../packages/essbatch/docs/batch.md#terms).
Two words name what this decision replaces.
A *scope* was a slice of an index that a source gave each row, kept in the header of a batch.
A *scan* asked the source for every row at each read of a batch.

Today the rows of a batch are stored nowhere. They are computed at each scan, and only what a person set on top is kept:

```python
scan = source.rows(files with corrections)       # code of the application
rows = scan in scope, with edits on top          # edits.json: added, edited, cleared, removed, by key
```

So a batch is defined by how its rows are made, and the table a person sees is the output of that recipe now. Six problems follow.

1. **The table needs the files.** If the file mount is not available, the scan gives no rows, and the table is empty. The results are in the workspace, but no row shows them.
2. **Removing files removes rows.** A row whose file is gone disappears, also one that was reduced or edited.
3. **A change of the source's code changes every batch**, also one that was finished last week.
4. **The model has no table to put a table in.** The command takes one (`essbatch reduce iofq.yaml runs.csv`), and the page loads and saves one. Both are translated into "cells set by hand on top of the scan", in four structures of different shape (`added`, `edited`, `cleared`, `removed`) and a marker for a cleared cell.
5. **A cell is held in two forms**: a pydantic object if a source or the page made it (`FileRef(file='run:60339')`), plain data if a table file did. Four functions convert between the forms and do not agree: a table file loaded back unchanged marks a cell that holds a reference to an output as set by hand.
6. **What a batch is depends on what amends it.** The source, the scope, and who wrote each cell are parts of every batch, because a scan must be able to run again at any time. A batch made from a table file has no source and carries the same parts.

A person thinks of a batch as a table: they edit its cells, remove its rows, reduce its rows and read their results.

## Decision

**A batch is a template and a table. The settings are one value per parameter, for the batch. The template holds the settings of the parameters that are no column, and its blanks are the columns; each row of the table fills it once. The value of a parameter for a row is its cell; if the cell is empty or the parameter is no column, the setting; if the batch has none, the default of the parameter. In a batch made from a source, a column that the source fills takes its value from the cell only. The table has one value per cell, and only `set_table` changes it: with what a person asks for, or with the rows that a step of Follow adds, which never changes a row that the table has (ADR 0013). The table is not computed again from the source at each read: how the table was made is not part of the batch.**

This decides what a batch is when it is made once and then changed by a person. How a batch is amended from the files, Follow included, is decided on top of it, in [ADR 0013](0013-follow-adds-the-rows-not-considered.md).

### Keys, columns, cells

- **The key** of a row is text, unique in the batch, and the index of the batch table. It joins a row to its results: the records under the label of the batch with that key.
- **A column named as a parameter of the spec is a parameter column**, and the parameter columns are the blanks of the template. Any other column, such as `run` or `name`, is a note for the person. Nothing else tells them apart.
- **A cell holds one value, or is empty.** The value is plain data: a number, text, a truth value, a list, or a group. A file and an output are text: `run:60339`, and `r17.centre` for the output `centre` of record `r17`.
- **A column has the type of its parameter**, so a wrong value is refused where it enters the table.

### A row fills the template

- **A cell overrides the setting for its row.** A column is a parameter that a row may give itself. The settings may hold a value for it, which every row with an empty cell takes, also a row added later.
- **An empty cell with no setting gives no value.** The parameter then takes its default, or `None` if it takes `None`, and the row is incomplete otherwise. A cell holds no word for `None`: in a table, empty is `None`.
- **In a batch made from a source, a column that the source fills takes its value from the cell only.** An empty cell there means that the row waits for its file, not "take the value of the batch": the row is incomplete, whatever the settings hold and whatever the default of the parameter is.

### The state is not stored

A row whose request differs from its result's only at settings reads `settings changed`, reduced with earlier settings: a change of settings applies to the rows not reduced yet. For one row, a parameter counts as a setting if it is no column or the row's cell is empty, since the row then follows the setting. A row whose own cell changed reads `outdated`. Which cells are empty is read from the table now, so nothing more is kept.

A cell that names a file that is not found is marked when the table is shown, and a request with it is refused, as for any cell that does not resolve. Nothing about the files is stored in the table.

## Examples

**The files are not reachable.** The page shows the table, the results and the plots. Each file cell is marked as not found, and "Reduce" on such a row is refused with that reason.

**Two files are removed.** The two rows stay as they are, with their results. Their file cells are marked.

**A run is corrected.** Run 60306 was recorded as a sample and is a transmission run. The batch has a row `60306`, and row `60304` names another transmission run. Nothing changes in the batch. The person removes row `60306` and sets the cell of `60304`.

## Not decided here: amending a batch from the files

Two conditions for it follow from this decision:

- **What amends a batch uses the table as a person does**: it adds a row or sets a cell with `set_table`.
- **What it needs to know is its own state, next to the batch**: which source, and which rows it has seen. None of it is a column of `rows` or a field of every row.

## Alternatives considered

- **Keep the rows derived, and cache the last scan for when the files are away.** Two models: what a person sees depends on whether the cache or the scan answered, and removing files still removes rows.
- **Two values per cell, one that the source gave and one that a person set, and a scan that merges.** It stores the inputs of a merge in place of the table. Each row then carries what only a scan needs (`source`, `hand`, `cleared`, `added`, `removed`), and the table is not one that pandas holds or stores. It also decides the three uses of a scan above with "yes" without asking.
- **A parameter is a setting or a column, never both**, as in a template: every value that the table contributes would be in the table, and no cell would depend on a setting. Making a parameter a column would copy the setting into the cell of every row and drop the setting. A row added later, by Follow, by "Add rows from the runs" or by hand, would have an empty cell, so it would be reduced with the default, not with the value that the batch had, and nothing would tell the person.
- **Fill the cell of a row added with the setting.** Each code that adds a row (a step, `add_rows`, the page, a table file) would need the settings, and a setting changed would not reach the rows added before.
- **A setting at a column that the source fills, as at any other column.** A row taken before its file arrives would take the setting, a file of another sample, and be reduced with it without a word.
- **The columns fixed when the batch is made** (today). Nothing needs it: a row is outdated by the comparison of its request with its record, whatever the columns were.
- **A list of the parameter columns in the header.** The template has it: its blanks.
- **A word for `None` in a cell**, such as the text `none`, so that an empty cell of `direct_beam` can mean "not said yet". A text column would hold a word that is not text of its parameter, and pandas reads empty as missing in every operation.
- **Cells as typed objects** (`FileRef`, `OutputRef`). Two forms of one value, and converters between them; see the context.
- **Rows of plain data inside the batch, and a `DataFrame` only at its interface.** With one value per cell the frame is the table, and a second form of it needs converters and a rule for which one is right.

## Consequences

- A batch is shown without a scan and without its source. A batch cannot be shown in two cases: the workspace binds no spec of its name, or it binds a version of the spec that refuses the cells of the table, since it takes another kind of value at a column, or has a parameter of the name of a note.
- A wrong value in a cell is refused when it is set or read from a table file, not when the row is reduced.
- A spec cannot make a person choose between a value and `None` for each row: an empty cell of such a column is the setting, or `None` if there is none. It still can for a setting.
- An empty cell depends on the settings, outside the table. The page shows it in grey as the value that the row takes.
- A table file shows an empty cell where a row takes the setting, so it needs the settings file to say what a row was reduced with.
- A cell emptied after its row was reduced counts its value as a setting: the row reads `settings changed`, not `outdated`.
- A row that only wrong metadata made stays until a person removes it, and a correction of a run changes no batch (the example above).
- The command and the page need no translation of a table: a table file is the table.
- Removing a row drops it. Its records stay under the label, as every record does, and show again if a row with that key is added.
