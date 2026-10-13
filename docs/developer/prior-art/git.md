# Git as prior art for records, references, and history

Git is not a reduction tool.
We read it because it is the best-known system built on two ideas our design shares: objects that never change, linked by references, and a few names whose meaning moves as objects are added.
Twenty years of users have recorded what that model costs.

| Git | Here | Note |
|---|---|---|
| commit | record | never changes; git addresses it by content hash, the backend by a random ID |
| parent link | the order of records under a label | git stores a link; the backend keeps the order the records were logged in |
| branch, its reflog | label and member, `client.latest` | git moves a pointer; a label is a query over records that are only appended |
| gitlink (submodule entry) | reference | names one object, never a name |
| reachability from refs | keepers ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md)) | see below: the rules differ |
| blob | output value | a blob is history; an output value is not ([ADR 0004](../adr/0004-history-is-append-only-lists.md)) |
| LFS pointer | record and store | the record names no file; the store keys a value by record ID and output name |
| push of the objects a ref needs | `client.publish` with provenance | the closure travels |

## Findings

**A history is ordered by links, not by clocks.**
A commit names its parents, and where order must be right, such as merge bases and `git log --graph`, git uses the topological level or a "corrected committer date" that is always greater than its parents' ([commit-graph.adoc L40-L96][cg]).
Plain timestamps are only a heuristic there: "A _probably_ cannot reach B", and the default `git log` order, reverse chronological by commit time ([rev-list-options L887][rlo]), may break parent order "due to clock skew".
Here, the records under a label come in the order they were logged, and `client.latest` returns the last of them ([client.py][latest]); `Record.created` is never used for order.
This holds because one process writes a proposal's history.
A second writer would appear in two open questions: an upgrade where old and new instance both run ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md), Open), and the layout of logs per proposal on the service ([#27][i27]).
Git's lesson for those: keep one append order per label, or have each record name its predecessor; do not fall back to `created`.

**A reference names an object, never a name.**
A submodule's gitlink holds the commit the superproject expects ([gitsubmodules L37-L38][sm]); the branch named in `.gitmodules` is a hint, which `submodule update --remote` follows.
`main@{yesterday}` reads the local reflog, so it means different things on different machines ([revisions L90-L100][revdate]).
Here, a dataset name such as `dataset(run=60339)` is resolved to the dataset's identity at submission, and the record holds the identity ([Datasets](../README.md#datasets)).
A reference points to an output of one record ([References](../README.md#references)); no reference means "the latest record under a label".
Keep it so.
A name resolved at submission gives a backlog or a re-run a different input than the live reduction got.
"The latest can before this sample" is a match against the sample run, which applications make ([#33][i33]).

**Git keeps what is reachable, and pays for it under concurrent writers.**
`git gc` keeps everything reachable from refs and reflogs and prunes unreachable objects after a grace period, two weeks by default ([config/gc L96-L105][gcexp]).
The grace period protects a concurrent process that is about to reference an object that is unreachable now, and the docs admit that "users who run commands concurrently have to live with some risk of corruption" ([git-gc L157-L173][gcnotes]).
Here, a reference keeps no value; only keepers do: the client that asked, a pending request until its workflow returns, a persist request, the store ([How long records and values are kept](../README.md#how-long-records-and-values-are-kept)).
A submission may reference only values its client keeps, persisted values, and datasets, so whether it is refused never depends on how far other work has come ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md)).
That rule removes the race git mitigates with a grace period.
Git's rule would also keep every intermediate of a chain for as long as its final output is kept.

**Content addressing does not transfer.**
Git can address by hash because the same content is the same object, wherever it was made.
A record is written once by one writer, so a random ID (`uuid4` in the backend) serves.
Running the same request again makes a new record ([Requests and records](../README.md#requests-and-records)).
Outputs are computed, and the same request is not guaranteed to give the same bytes on two machines, so a hash would give one result two identities.

**Keep large data out of the history from the start.**
Git stores every version of every file in its object store, and a full clone copies them all.
Git LFS, a separate project with its own server, began in 2013, eight years after git.
An LFS pointer is a small text file holding a `sha256` object ID and a size ([LFS spec][lfs]).
Here, history names no file, and the store keys a value by record ID and output name ([ADR 0002](../adr/0002-a-value-lives-while-something-keeps-it.md)), so the separation is there already.
The file format and the store's layout are open (ADR 0002, Open); a hash and size per written output would let the store detect a damaged or truncated file.

**Publishing sends a closure.**
`git push` updates remote refs and "sends all necessary data that isn't already on the remote" ([git-push L21-L23][push]), so the remote can read every commit the ref reaches.
`client.publish` (not implemented) puts an output in a catalogue with its provenance: every record that supplied an input, down to the datasets ([Provenance and publication](../README.md#provenance-and-publication)).
The catalogue entry survives the proposal's history for the same reason a pushed branch survives the local clone: what it needs travels with it.

**Names need a namespace.**
Git keeps branches, tags, and remote branches under separate prefixes (`refs/heads/`, `refs/tags/`, `refs/remotes/`), resolves a short name by a fixed order of rules, and warns when it is ambiguous ([revisions L27-L33][revname], `core.warnAmbiguousRefs`, true by default, [config/core L407-L409][warn]).
Here, a label is one free string per proposal.
A rule owns its label by convention only: a record a notebook submits under it stops the rule from reducing the datasets that record names ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).
Labels are tentative ([open question 3](../README.md#open-questions)).
If they stay, separate the names of rules from the names people choose before the first users, since renaming names that records already carry is costly.

**The interface leaked the model.**
Perez De Rosso and Jackson trace git's usability problems to its concepts, such as the staging area and detached HEAD, not to its commands ([Onward! 2013][wwg]).
Their redesign, Gitless, commits changes to tracked files without a staging step, and lets branches carry uncommitted changes ([README][gitless]).
Here, the client carries records, labels, references, and keepers.
The first-release application ([requirements, Goal](../../requirements/README.md#goal)) must let a scientist select a workflow, run it, look at results, and upload them without thinking in those terms.

**Automation belongs next to the versioned content, and its status on a server.**
Git hooks live in `$GIT_DIR/hooks`, outside the tracked tree ([gitrepository-layout L170-L178][hooks]), so a clone does not carry them.
CI services read their configuration from a tracked file, show the status of each run to anyone with access, and leave re-running failed jobs to a person, with the same commit as the first run ([GitHub docs][rerun]).
The trigger loop matches the last point: a failed record counts as handled, and running it again is the user's decision ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).
It differs on the second: a rule's status lives in the `TriggerLoop` object, which an operator in another process cannot read ([#24][i24]).
This bears on the open question in [#33][i33], whether rules are plain data that a driving server can list.

**Where git's model does not apply.**
Git merges histories by text.
Here, two variants are two labels, and combining runs is an accumulator adding rows ([ADR 0006](../adr/0006-the-unit-is-an-accumulating-workflow.md)), not a merge.
Git is distributed with no single writer; here one process writes a proposal's history.

## Borrow

- Order under a label by the order logged, or by a link to the previous record; never by time.
- References to identities, never to names that move.
- Separate namespaces for rules' labels and people's labels, if labels stay.
- A hash and size per persisted output, when the store's layout is designed.
- Rule status kept where other processes can read it.

## Do not borrow

- Reachability as the rule that keeps values, and grace periods against concurrent writers.
- Content addressing for records or outputs.
- A mutable head pointer with a reflog: a label stays a query over records that are only appended.

[cg]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/technical/commit-graph.adoc?plain=1#L40-L96
[rlo]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/rev-list-options.adoc?plain=1#L887-L891
[latest]: ../../../packages/essdispatch/src/ess/dispatch/client.py
[sm]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/gitsubmodules.adoc?plain=1#L37-L38
[revdate]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/revisions.adoc?plain=1#L90-L100
[revname]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/revisions.adoc?plain=1#L27-L33
[warn]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/config/core.adoc?plain=1#L407-L409
[gcexp]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/config/gc.adoc?plain=1#L96-L105
[gcnotes]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/git-gc.adoc?plain=1#L157-L173
[push]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/git-push.adoc?plain=1#L21-L23
[hooks]: https://github.com/git/git/blob/6de20f6092dcf9bdb1c8efe03db4b70c82b423dd/Documentation/gitrepository-layout.adoc?plain=1#L170-L178
[lfs]: https://github.com/git-lfs/git-lfs/blob/0043a645047926f4bd7f7091299095528253d575/docs/spec.md#the-pointer
[wwg]: https://dspace.mit.edu/handle/1721.1/105233
[gitless]: https://github.com/gitless-vcs/gitless#readme
[rerun]: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs
[i24]: https://github.com/scipp/essapps/issues/24
[i27]: https://github.com/scipp/essapps/issues/27
[i33]: https://github.com/scipp/essapps/issues/33
