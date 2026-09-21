# What git says about the sketch

> Unmaintained study, written against the edition of the design at commit `cc4ad3a`.
> Decision numbers (D1 to D15) and links to `architecture.md`, `staging.md`, and `stateless.md` refer to that edition.
> The lessons taken from it are in the [current documents](../README.md).

Companion to [architecture.md](architecture.md) and [mantid.md](mantid.md).
Git is not a reduction tool, and the parts of the sketch that came from Mantid and FIA, the lookup, the rule, the label, the batch table, and the reprocess, have no counterpart in it.
It is read here because it is the best-known system built on the same two ideas the sketch rests on: immutable objects linked by references, and a small set of named pointers that move; and because twenty years of users have recorded what that model costs when it reaches a person.
Analysis, then decisions: the last section says what was folded into the sketch, what was proposed and withdrawn, and why.

Two of git's layers need separating first.
The object graph, commits and trees linked by hashes, maps onto records and references.
The blobs, the bytes at the leaves, do not map onto outputs: git never recomputes a blob, so everything it does about storage is about never losing one, while the sketch's outputs are derived and may be dropped and recomputed (D1).
For outputs the nearer prior art is a build system, Nix or Bazel, and nothing from that side is taken here.

## Git in the sketch's terms

| Git | Here | Note |
|---|---|---|
| Commit | Run record | Immutable, addressed by identity, carries author, date, and message; a UUID here instead of a hash |
| Parent pointer | The supersedes link under a label | Provenance follows references; git adds history under a name |
| Blob | An output's bytes in the data store | Terminal in git, derived here |
| Branch | Label, with its member key | A name whose target moves |
| Tag | A published PID (D11) | A name that never moves, carrying its own metadata |
| Reflog | The records under a label, in order | Git keeps it per ref; here it is a query |
| Notes | Annotations | Attached beside the object, never inside it, read by nothing |
| Working tree, index, commit | Apply, validate, submit | Nothing exists until the last step |
| Config layers with `--show-origin` | Template, lookup entry, typed values; the submission names which applied | The same ladder |
| Rebase | Reprocess under a new template or lookup version | Typed values are the patch, the template and lookup are the base |
| Push and fetch of a reachable closure | The deferred upload of records from a local store | Refs stay local; objects travel |
| Hooks | The trigger loop | Git's are unversioned scripts; the industry replaced them with CI configuration in the repository, which is the sketch's rule as versioned data |
| LFS pointer | The record saying an output exists, the data store holding bytes | Hash and size in the pointer, bytes elsewhere |

## What transferred

**A name's history is a chain, not a clock.**
Git never orders a branch's history by commit time; each commit names its parent, and the tip is the commit nothing points at.
It learned this because clocks on contributors' machines disagree, and a history that depended on them would reorder itself.
The sketch defined the latest record under a label and member key by creation time, and the skeleton implemented it that way.
That holds in local mode, where one process makes every record, and breaks in shared mode: the trigger loop, a person correcting a member, and a retry of a failed record can all submit under one label and member key, from different hosts, and a person's correction can lose to a retry stamped a second earlier by a slower clock.
Git's answer is one field: the record names the record it supersedes, filled by the backend at submission with the latest under the label and member key at that moment.
The backend is the single writer (D5), so two submissions against one head are serialized and the second supersedes the first; git has to refuse a non-fast-forward push, the sketch does not.
The latest record is then the one nothing supersedes, a query as before, and the diff a slot shows (D10) is against a named record.
A retry supersedes the failed record it derives from, and a chained combine supersedes the previous combine it references, so the field is new only for a correction, a slider move, and a manual member under a rule's label.
Two links stay distinct, as in git: the supersedes link says where a record sits in a name's history, and the derivation link says why the request was made.

**Reprocess is a rebase, and a rebase can conflict.**
The precedence ladder (D14) makes typed values a patch over the template and lookup, and the reprocess replays the patch over a new base: `git rebase`.
Git's three-way merge flags the case where the base changed a line the patch also changed, instead of silently taking either side.
The sketch's ladder always let the typed value win, so a Q range a user typed for one member silently shadowed a Q range the instrument scientist corrected in the lookup for that angle.
The reprocess now flags the members where a typed value shadows a field whose fill changed between the versions, a three-way compare of old fill, new fill, and typed value over plain JSON, and the person decides.

**Names need a namespace.**
Git keeps branches, tags, remote branches, and notes under separate prefixes, warns when a bare name is ambiguous, and still spent years on the mess of `refs/` before that settled.
The sketch's label was one free string: a rule's records carried the rule's name, a person picked a name for a batch, and nothing stopped a person from naming a batch after a rule and appearing in its table.
A rule's label is now reserved: the backend refuses a request under it that the rule did not fill, and a missed run or a corrected member goes through apply, which fills it.
Git says decide before the first user, because renaming refs later is what its ref namespace history is about.

**A name as a stand-in was proposed, and the discussion replaced it.**
A git checkout of `main` resolves the name to a commit at that moment and records the commit, and the sketch resolves stand-ins at submission (D1), so "output X of label L" looked like a fourth stand-in, for a processed vanadium that a template names and that changes now and then.
Cans, dark frames, and empty-beam runs are measured many times per experiment, and for them the semantics are wrong: samples 1 to 10 measured after can A and 11 to 20 after can B need A and B respectively, and a label resolved to its latest at submission gives every backlogged or reprocessed sample can B.
What they need is an as-of match, the nearest earlier dataset with the right role, which is what FIA did by walking the journal and what the pandas picture already called `merge_asof`.
The lookup now has an as-of fill (D14), resolved against the member rather than the clock, so the backlog and a reprocess give a sample the same can the live loop gave it.
The label stand-in and the revert that went with it, a copy record moving a label back to an older result, were withdrawn with it, since without label stand-ins nothing resolves against a label's head but a slot, and a slot goes back by rerunning.

## Proposed and not taken

These were listed in the first draft of this note because git suggested them, and were cut on review as cheap things without a user, or as wrong.

- **A digest of the resolved request**, so that validate can say "identical to completed record X", git's "nothing to commit". Not the identical-request reuse the second pass removed, but one step from it, and no one has asked for the message. Take it when a UI does.
- **Human addressing**, a record by label and offset or time and a UUID by prefix, git's `main~3` and short hashes. Useful the day a notebook user types a record ID; not before.
- **A status vocabulary per dataset**, `git status` over a rule's datasets. A UI column, not a model change.
- **Hash and size on every disk copy**, the LFS pointer. Integrity and upload dedup, with the caveat that scipp HDF5 need not be byte-deterministic; take it with the upload.
- **Bisect over a label.** Falls out of the supersedes link and a diff of resolved requests; a client operation for later.
- **Content digests as template, lookup, and rule versions.** Wrong: the reprocess needs "records whose latest came from an older version", and a digest has no order; a counter or a chain does.
- **Git as the store for templates, lookups, and rules.** Attractive for instrument defaults, which are version-controlled files already; a web UI that commits is a design of its own, and the mutable state on a rule would live elsewhere in any case. Decide when the store for them is built.
- **An explicit, mutable head pointer with a reflog.** Git's actual mechanism; the supersedes link gives the same answers as a query over immutable records, and the sketch has resisted every second copy of truth.
- **Reachability collection.** Rejected in the second pass; git's experience with it says nothing to reopen. Its eviction lesson runs the other way: a superseded combine's partial is reachable from the head yet needed by nothing, and the sketch's evict-superseded-first is better than reachability for derived data.

## What git got wrong, to avoid

**The interface leaked the model.**
The index, a detached HEAD, and the fear of rebase are the model showing through the porcelain, and are what people mean when they say git is hard.
The sketch's plumbing, the client interface, carries supersedes links and label queries; the batch table, the slot, and the record browser must not ask a scientist to think in them.

**Time is untrustworthy, and git still shows it.**
Commit dates are wrong across machines often enough that `git log` orders by parent and only displays the date.
Show a record's time; never sort by it where a chain exists.

**Large files were bolted on.**
Git kept blobs in the object store, failed at large binaries, and added LFS a decade later as an external pointer scheme with its own server.
The sketch separates the record from the bytes from the start (D3).

**Hooks were left out of the repository.**
Because git does not version hooks, every team reinvented a way to ship them, and CI configuration in the repository is the shape that won.
The sketch's rule is versioned data from the start; the CI systems built on git also have, under other names, the sketch's cancel-in-progress slot, its rerun of failed members, and its manual apply, which says the rule's operations are the expected set.

## Where the model does not transfer

Git merges content, and its merge is textual.
The sketch never merges a record: two variants are two labels and stay so, and combining a series (D15) is a fold over contributions, not a merge of histories.

Git is distributed with no single writer, and its object model is what makes that safe.
The sketch has one writer per store (D5) for reasons esslivedata paid for; the push and fetch of reachable closures is the only distributed piece it needs, and it is what the deferred upload and the two-notebooks question already describe.

Git addresses everything by content.
For records a UUID serves, since a record is written once by one writer; for outputs, bytes are derived and may differ across environments, so content addressing would give two identities to one result.

## What was folded into the sketch

| Lesson | Change to the sketch | Where |
|---|---|---|
| A name's history is a chain | The record names the record it supersedes, filled by the backend at submission; the latest is the record nothing supersedes; the slot diff is against it | Records; D10; Components; Glossary |
| Rebase conflicts | The reprocess flags members where a typed value shadows a field whose fill changed | D14 |
| Namespaces | A rule's label is reserved; a missed run or a corrected member goes through apply | D14; Components; Glossary |
| A name as a stand-in, withdrawn | The as-of fill in the lookup, resolved against the member, for cans, dark frames, and empty-beam runs | D14; Glossary |

All four are in the skeleton: the `supersedes` field and the head queries in the record store, the reserved label in the backend's validation, `shadowed` beside `reprocess`, and `AsOf` as a lookup fill.

## Sources

- `gitglossary`, `gitrevisions`, `git-rev-parse`, `git-notes`, `git-config` (`--show-origin`), `git-gc` (`gc.pruneExpire`, `gc.reflogExpire`), `git-submodule` (`branch` tracking), `git-commit` (`--allow-empty`), `git-revert`
- The [git-lfs pointer file specification](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md)
