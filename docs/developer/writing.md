# Writing and rewriting text in this repository

For anyone, person or agent, who writes or rewrites a docs page, a docstring, a comment, an error message, help text or an ADR here.
After reading you can decide whether a text should exist, where it belongs, how to word it, and how to rewrite existing text in passes.

The problem this page answers: a third of our source lines are prose, and readers still find it confusing.
Most of that prose repeats code, repeats other prose, or uses ordinary words in private senses.

## 1. Should this text exist?

Take the first row that fits. Prose is the last resort.

| What you want to state | Say it with | Example |
|---|---|---|
| what a function does, step by step | the code; make it shorter or name its parts | — |
| what a parameter or attribute means | its name | `also` → `stage_reads` removes a 6-line paragraph |
| the parts of a tuple | a dataclass or `NamedTuple` | `acc.steps.pop()[1]` → a named field |
| the allowed values of a string | `Literal` or an enum | the eight statuses of a row |
| "`None` means not compared" | a type of its own, or two functions | — |
| a precondition ("lock held", "not empty") | one stated convention, a name suffix, or an `assert` | 46 docstrings say "lock held" |
| a consequence of a rule ("So a removed row stays away") | a test whose name is that sentence | `test_a_row_that_a_person_removes_stays_away` |
| what went wrong and what to do | the error message | — |
| why it is built this way, what was rejected | an ADR, linked from the module | — |
| status, plans, open questions, measurements | a GitHub issue | — |
| what a text used to say, what was removed | the commit message | — |

What is left for prose:

- **What a reader would guess wrong.** "A step does not retry a failed row" is worth a sentence. "`| None` does not change the dtype" is not.
- **Contracts at a boundary.** What a caller may rely on and what is raised, where the signature does not show it.
- **What spans functions.** Threading rules, the order of writes that makes a crash safe. Once, at the top of the module.
- **A text format** that no type shows, such as `'r17.centre'` for an output in a cell.

Do not write:

- lists of rules or invariants, and names of functions or tests "where it is enforced"
- lists of the callers of a function
- claims about the whole code base ("the only writer", "no other code converts")
- what something is not, unless a reader would assume it

## 2. Where each text lives

Every statement has one home. Other places link to it and do not repeat it.

| Home | Reader | Holds | Does not hold |
|---|---|---|---|
| guide page (`.md`) | a user of that interface | purpose, one example, the concepts, what they would guess wrong; a sketch of a mechanism, as pseudo-code or a Mermaid diagram | material for another reader, a retelling of functions |
| module docstring | a maintainer of the module | what spans its functions, in words; a link to the guide section with the sketch | the interface, a sketch or drawing, a retelling of functions |
| class or function docstring | a caller | first line: what it is or returns; parameters and errors the signature does not show | mechanism, consequences, reasons |
| private function | a maintainer | one line, or none if the name says it | — |
| comment | a maintainer at that line | a reason the code cannot show | what the next line does |
| ADR | someone who wants to change a decision | the problem, the decision in one paragraph, each rejected alternative in one line | interface, file layout, mechanism, a changelog |
| test name | someone asking "is this on purpose?" | one behaviour, as a sentence in everyday words | — |
| error message | the person who hit it | where, what is wrong, what to do | the rule behind it |
| `--help` | a person who runs the command | one example per task | design terms |

A sketch of how a mechanism works lives in a docs page, since a docstring cannot hold a drawing. The module docstring links to that section.
A guide section that is a copy of a module docstring, of `--help` or of an ADR is deleted, not polished.

## 3. Words

**A term is a code identifier in backticks or a distinctive compound.** Nothing else is a term.

- `Batch.put`, `Step.seen_keys`: the backticks mark it, and the code defines it.
- "batch table", "row source", "files table": cannot be mistaken for ordinary use.
- "row", "column", "table", "key", "put", "take" keep their everyday meaning. If "row" meant a row of the batch table, we could not write about the rows of any other table.

**One name per thing, one meaning per word,** in prose and in code. Not "batch table / frame / table / rows" for one object. Not "keep" for hold in memory, store on disk, and retain.

**Use the standard verb.** Our texts use everyday verbs in private senses. Replace them:

| Instead of | Write |
|---|---|
| the function *gives* | returns |
| the function *takes* | accepts; has a parameter |
| the row *reads* `earlier` | has status `earlier` |
| the page / the table *says* | shows; contains; states |
| the route *answers* | returns |
| a request *writes* `file(run=611)` | specifies |
| X *is refused* | `f` raises `ValueError`; the page rejects |
| the workspace *keeps* | holds in memory / stores on disk (pick the one meant) |
| a cell *names* an output | refers to |

Define a term once, where it is first needed, and then use it. Do not paraphrase a term to avoid it.
A terms table lists only terms used across sections, and no class names.

## 4. Sentences

One idea per sentence. A first sentence has at most 25 words. Then check for these, each with a real example from our texts:

| Pattern | Before | After |
|---|---|---|
| chain of "of" and "that/whose" | "the rows of the source whose keys are not considered" | name it or use code: "new rows: `key not in considered`" |
| ellipsis | "So is reading the latest state already added" | repeat subject and verb |
| negative as a noun | "parameters that are no column" | "parameters without a column" |
| content in parentheses | "(`batch.empty()`, which `batch.cells()` and so the state read)" | its own sentence, or delete |
| "but" for "except", "so" for "this way" | "each model in it, but a reference, as a dict" | "each model except a reference, as a dict" |
| rule, then a clause that reverses it | "A role is never refused … At a file of another kind, a role is checked" | state the scope first; the exception gets its own sentence |
| a function name as grammar | "`put` a frame with a column" | "call `batch.put(frame)`" |
| abstract subject | "A refused reduce makes no batch" | "If `reduce` fails, no batch is created" |

## 5. Shape

**A page**

- The first lines say who it is for and what they can do after reading.
- Order: purpose, one example, concepts, reference. Edge cases and failure windows go in one section at the end.
- One running example, introduced before it is used, with the same values everywhere.
- A heading says what the section contains.
- A table cell has at most about 12 words. Longer content is a paragraph.
- No status banners, "not implemented" notes, open-question lists or measurements. They go to issues.

**A docstring**

- The first line says what the thing is or returns, on its own line.
- Describe a parameter under `Parameters` and an attribute at the attribute, only where name and type do not say it.
- Refer to code by plain name in double backticks. Use a Sphinx role only for a public name in another module; the docs will be built with Sphinx (and MyST for Markdown).
- Prefer an input → output example to a description:

```python
shape(float | None)
# FieldShape(none=True, inline=Scalar(float), referenced=(), metadata=())
```

## 6. Rewriting existing text, in passes

Deep problems hide shallow ones. A sentence polished in a section that should not exist is wasted work, and a sentence pass run first spreads the old vocabulary.
So run the passes in this order, one pass per commit, and do not do the work of a later pass early.

| Pass | Question | Typical action |
|---|---|---|
| 0. Code | Which prose exists only because a name or a type is weak? | rename, dataclass, `Literal`, lock convention (section 1) |
| 1. Reader and home | Who is this page or docstring for? What here is for another reader, or is a copy? | delete or move whole sections; status to issues |
| 2. One home | Which statements repeat code, a test, a message, or another text? | delete; turn a consequence into a test if none exists |
| 3. Shape | Does the order serve the reader? Is the example first and consistent? | reorder, fix headings, structure docstrings (section 5) |
| 4. Words and sentences | Section 3, then section 4 | reword |
| 5. Check | Did anything break? | see below |

Rules for every pass:

- **Never delete a statement of behaviour unless code, a test or an error message carries it.** Find the test first. If there is none, write it. If unsure whether a sentence carries weight, keep it and list it.
- **Pass 0 renames freely, public names included.** Nothing outside this repository uses the code. A rename changes every use: code, tests, docs, messages, help, and the names in files on disk.
- **Check (pass 5):** the tests pass. Grep the repository for links to and mentions of every heading, term and name you removed. Every remaining statement is true of the code today; a second reader checks this against the code.
- **Report** per pass: lines and words before and after; each deleted consequence with the test that covers it; statements kept because unsure; where these rules did not fit.

Then run the passes again. A first round removes what hides the next layer; stop when a round changes little.

## 7. Models

Texts in this repository that work, to imitate:

- `docs/developer/README.md`, "From a for loop": the same task before and after, then one line per new name.
- `packages/essspec/docs/params.md`, the first example: one spec seen as the spec, the request, and what the workflow receives.
- `packages/essspec/docs/bindings.md`, "Edge cases": collected in one section.
- `ess.apps.protocol`, `Store.read`: a contract as a 9-line table.
- `ess.spec.field_types`, `shape`: input and output as code.
- `docs/developer/adr/0010` and `0014`: a decision in 49 and 65 lines.
- Comments that carry a reason: "The position comes first, so that no finish after it is missed."
