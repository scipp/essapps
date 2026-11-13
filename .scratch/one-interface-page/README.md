Scripts behind `.scratch/next-steps/one-interface-page.md`. Run each with
`/workspace/essapps/.scratch/venv-container/bin/python <script>` from this folder,
on the checkout at `9c28338` (branch `184-automatic-as-batch`, editable install).

| File | What it does | Output |
|---|---|---|
| `harness.py` | files (`FakeFiles`), workspace with a store, the branch's `Batch` on toy `PLAIN`, `create_app`, the loop in a thread under `server.lock` | |
| `browser.py [N]` | headless Chromium on the page while files arrive: situations of section 1 | printed |
| `http_checks.py` | the server while the loop runs: version for a waiting row, retry, edits, load a table, slow reductions | printed |
| `scale.py N` | section 2 at N rows; `PATCH=1` replaces `forms.cell` by a lookup, `NOBROWSER=1` stops before the browser | `scale_*.out` |
| `state_as_is.py N` | one `Server.state()` as is after `_relist()` | `state_as_is.out` |
| `profile_relist.py N` | the profile that shows `forms.cell` | printed |
| `probe.py [basic\|virtual]`, `probe/index.html` | Tabulator alone at 300 to 100 000 rows | `probe.out`, `probe_virtual.out` |
