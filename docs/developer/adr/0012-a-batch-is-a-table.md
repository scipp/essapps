# ADR 0012: A batch is a template and a table

- Status: accepted
- Deciders: Simon
- Date: 2026-11-19

## Context

The terms are those of [batch.md, "Terms"](../../../packages/essbatch/docs/batch.md#terms).
*Template*, *blank* and *fill* are those of `ess.apps.Template`: a spec, some values, and blanks that each use fills.

Before this decision, the rows of a batch were stored nowhere.
Each read of a batch asked the source for every row (a *scan*).
It kept the rows in the batch's *scope*, a slice of an index that the source gave each row.
Then it applied what a person had edited, stored in `edits.json` as added, edited, cleared and removed cells, by key.
A batch was defined by how its rows are made, and six problems followed:

1. **The table needed the files.** Without the file mount, the scan returned no rows and the table was empty, although the results were in the workspace.
2. **Removing files removed rows**, also rows that were reduced or edited.
3. **A change of the source's code changed every batch**, also one finished last week.
4. **A table had no place in the model.** The command accepts a table file (`essbatch reduce iofq.yaml runs.csv`), and the page loads and saves one. Both were translated into cells set by hand on top of the scan, in four structures of different shape and a marker for a cleared cell.
5. **A cell had two forms**: a pydantic object such as `FileRef(file='run:60339')` if a source or the page made it, plain data if a table file did. Four functions converted between the forms and did not agree.
6. **What a batch was depended on what amends it.** The source, the scope and who wrote each cell were part of every batch, also of one made from a table file, since a scan could run again at any time.

A person thinks of a batch as a table: they edit its cells, remove and reduce its rows, and read their results.

## Decision

**A batch is a template and a table. The settings hold one value per parameter, for the whole batch. A column named as a parameter of the spec is a parameter column, and any other column is a note. The parameter columns are the blanks of the template, and each row fills the template once. The value of a parameter for a row is its cell. If the cell is empty or the parameter has no column, the value is the setting, and if the batch has no setting, the default of the parameter. Only `set_table` changes the table. The table is stored, and how it was made is not part of the batch.**

- **A key is text, unique in the batch, and the index of the table.** It joins a row to its results: the records under the batch's label with that key.
- **A cell holds one value of plain data, or is empty.** A file and an output are text, such as `run:60339`, and `r17.centre` for the output `centre` of record `r17`.
- **A column has the dtype of its parameter**, so a wrong value is rejected where it enters the table.
- **An empty cell is `None`.** A cell has no word for `None`. With no setting, an empty cell means the default of the parameter, or `None` if the parameter accepts `None`. Otherwise the row is incomplete.
- **A column that the source fills gets its value from the cell only.** In a batch made from a source, an empty cell there means that the row waits for its file. The row is incomplete, whatever the settings and the default hold.
- **The state of a row is computed at each read**, from the table, the settings and the records. A row whose request differs from its result only at settings has status `settings changed`. A row whose own cell changed has status `outdated`. A parameter counts as a setting for a row if it has no column or the row's cell is empty.
- **The table stores nothing about the files.** A cell that refers to a file not found is marked when the table is shown, and a request with it is rejected.
- **Whatever amends a batch uses `set_table`, as a person does.** What it needs to know, such as which rows it has seen, is stored next to the batch, outside the table. [ADR 0013](0013-follow-adds-the-rows-not-considered.md) decides how Follow amends a batch from the files.

## Alternatives considered

- **Keep the rows derived, and cache the last scan for when the files are away.** What a person sees would depend on whether the cache or the scan answered, and removing files would still remove rows.
- **Two values per cell, one from the source and one set by a person, merged by a scan.** Each row would carry what only a scan needs, pandas could not hold the table, and it would answer "yes" to each use of a scan in ADR 0013 without asking.
- **A parameter is a setting or a column, never both.** Making a parameter a column would copy the setting into every cell and drop it, so a row added later would be reduced with the default, and nothing would tell the person.
- **Fill the cell of an added row with the setting.** Each code that adds a row would need the settings, and a changed setting would not reach the rows added before.
- **A setting at a column that the source fills, as at any other column.** A row added before its file arrives would be reduced with the setting, a file of another sample, without a word.
- **The columns fixed when the batch is made.** Nothing needs it: a row is outdated by comparing its request with its record, whatever the columns were.
- **A list of the parameter columns in the header.** The blanks of the template are that list.
- **A word for `None` in a cell**, such as `none`. A text column would hold a word that is not text of its parameter, and pandas reads empty as missing in every operation.
- **Cells as typed objects** (`FileRef`, `OutputRef`). Two forms of one value need converters between them (problem 5).
- **Rows of plain data inside the batch, and a `DataFrame` only at its interface.** With one value per cell the `DataFrame` is the table, and a second form needs converters and a rule for which form is right.

## Consequences

- A batch is shown without its source and without the files. Rows whose files are missing stay, with their results, and their file cells are marked.
- A batch cannot be shown if the workspace binds no spec of its name, or a version of the spec that rejects the cells: another dtype at a column, or a parameter of the name of a note.
- A note cannot have the name of a parameter: a column `sample` is the parameter `sample`.
- A spec cannot make a person choose, for each row, between a value and `None`. In such a column, an empty cell means the setting, or `None` if there is none.
- An empty cell depends on the settings, outside the table. So a table file needs the settings file to show what a row was reduced with.
- A cell emptied after its row was reduced counts its value as a setting, and the row has status `settings changed`.
- A row made from wrong metadata stays until a person removes it, and a correction of a run changes no batch.
- Removing a row keeps its records under the label. They show again if a row with that key is added.
- `set_table` writes the whole table at each change.
- A table file is the table, so the command and the page translate nothing.
