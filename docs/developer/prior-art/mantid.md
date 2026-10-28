# Mantid's ISIS batch interfaces and FIA

Mantid is the data-reduction framework of ISIS, SNS and other neutron facilities.
Its ISIS Reflectometry interface is a Qt window in which a user fills a table of runs, configures the reduction, and processes the table, by hand or automatically as new runs arrive (*autoprocessing*).
The ISIS SANS interface is a similar table; ISIS Powder is a scripting interface with no GUI.
FIA (Flexible Interactive Automation) is ISIS's current automatic-reduction service; it runs a Mantid script per new run on a Kubernetes cluster.
Its predecessor, ISIS autoreduction, ran from 2015 to 2022.

We looked at them because they are batch and automatic reduction at a neutron facility, in daily use, with public code and issues.
The findings bear on batch and automatic reduction (scipp/essapps#24, scipp/essapps#33) and the service (scipp/essapps#27).
The reflectometry interface is living code: about 40 commits to it in the year to 2026-10, many of them build changes, and a plotting tab added in 2026-09 ([mantid#41637](https://github.com/mantidproject/mantid/pull/41637)); autoprocessing gets fixes, not features.
ISIS reflectometry is automated only through this interface: FIA does not reduce reflectometry runs (see the last finding).
Facts were checked on 2026-10-13 against Mantid [`0a13770`](https://github.com/mantidproject/mantid/tree/0a137707b24563d2e5d690951651fcee052ce635), FIA's [run-detection `881dacd`](https://github.com/fiaisis/run-detection/tree/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397), [FIA-API `9707aa0`](https://github.com/fiaisis/FIA-API/tree/9707aa094c4e4a9c96f31142e8777f2e0c430273) and [file-watcher `bb91013`](https://github.com/fiaisis/file-watcher/tree/bb9101322fc485b15cc16e448e705ec42f614ac0), and the archived [autoreduction](https://github.com/autoreduction) repositories.

## The reflectometry interface in brief

A *row* is one reduction: sample runs (summed if several), an angle, transmission runs, a Q range and step, a scale, and an *options* column of free `key=value` algorithm properties.
Rows sit in *groups*; a group with more than one row is stitched once all its rows succeed ([Group.cpp L59-L63](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/Group.cpp#L59-L63)).
A row's algorithm properties are set in this order, later overwriting earlier ([RowProcessingAlgorithm.cpp L400-L416](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Batch/RowProcessingAlgorithm.cpp#L400-L416), [L73](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Batch/RowProcessingAlgorithm.cpp#L73)):

```
event, experiment and instrument settings  ->  matching lookup entry  ->  row cells  ->  options column
```

Autoprocessing searches the run journal of one investigation every 30 s ([CatalogRunNotifier.h L23](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Runs/CatalogRunNotifier.h#L23)).
It reads the angle from `th=` in each run title and takes the text before it as the group name ([ParseReflectometryStrings.cpp L208](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/ParseReflectometryStrings.cpp#L208)).
The whole batch, including the table with row states, the search results and the lookup table, saves as one JSON file ([Encoder.cpp](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Common/Encoder.cpp)).

## Findings

**Failed rows are never retried automatically, so a run listed before its file exists fails for good.**
The interface's documentation says so ([ISIS Reflectometry.rst L646-L650](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/docs/source/interfaces/reflectometry/ISIS%20Reflectometry.rst#L646-L650)), and each poll processes with failed rows skipped ([BatchJobManager.cpp L80-L98](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Batch/BatchJobManager.cpp#L80-L98)); the user selects failed rows and clicks Process to rerun them.
In 2024, on ISIS's analysis machines (IDAaaS), the journal began to list runs seconds before their files reached the data cache, so autoprocessing failed those rows and never came back to them ([mantid#36860](https://github.com/mantidproject/mantid/issues/36860)).
Retrying failed rows at each poll was considered and rejected as confusing for users.
The fix lists only runs found in the data cache's index ([mantid#37449](https://github.com/mantidproject/mantid/pull/37449)), together with slower polling ([mantid#36895](https://github.com/mantidproject/mantid/pull/36895)).
For us: the trigger loop makes the same choice, a failed record counts as handled ([automatic-reduction.md](../automatic-reduction.md#the-trigger-loop)).
So the file source must list a run's file only once it is complete; otherwise the first reduction fails and the file is never reduced again without a person.
This bears on the Open question of how long after the file writer finishes the catalogue lists a run ([data](../../requirements/data.md#open)).

**Typed and computed values are kept apart.**
When a row's Q range is blank, the reduction computes one.
The row keeps it in a separate field (`m_qRange` and `m_qRangeOutput`, [Row.h L72-L73](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/Row.h#L72-L73)), the batch file saves both ([Encoder.cpp L216-L217](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Common/Encoder.cpp#L216-L217)), and the table shows the computed value in the blank cell, styled as an output ([JobsViewUpdater.cpp L22-L36](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/RunsTable/JobsViewUpdater.cpp#L22-L36)).
For us: a value the workflow computes is an output of the record, not a parameter, and an application may show it next to the blank.

**The lookup table selects defaults by angle and title.**
An entry has an optional title regex, an angle (or none, for the wildcard entry), and defaults: transmission runs, Q range, scale, detector regions.
A row takes the entry whose regex matches its group name and whose angle is within the tolerance, else an entry without regex at that angle, else the wildcard ([LookupTable.cpp L58-L94](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/LookupTable.cpp#L58-L94)).
Validation of the table refuses a second wildcard and two entries with the same regex and angle ([LookupTableValidator.cpp L48-L65](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Experiment/LookupTableValidator.cpp#L48-L65)).
Two different regexes that both match one title are found only when the row is processed, and the row fails with an error ([BatchJobManager.cpp L217-L227](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Batch/BatchJobManager.cpp#L217-L227)).
The table shows which entry each row used, and the log warns when a row falls back to the wildcard or to algorithm defaults ([RowProcessingAlgorithm.cpp L257-L273](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Batch/RowProcessingAlgorithm.cpp#L257-L273)).
For us: the lookup belongs to the reflectometry interface, not to Mantid's framework, which is where scipp/essapps#33 puts pairing criteria.

**ISIS picks per-run settings in a different way in each tool.**
Reflectometry uses the lookup table above.
ISIS Powder picks vanadium, empty and calibration files by run-number range from a YAML file ([yaml_parser.py L50-L67](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/scripts/Diffraction/isis_powder/routines/yaml_parser.py#L50-L67)).
ISIS autoreduction took an experiment's values, else the set with the highest start run not above the run ([records.py L135-L153](https://github.com/autoreduction/queue-processor/blob/4eab9dea17df1fe599a5fcf23a77ea1e2554de2e/autoreduce_qp/model/database/records.py#L135-L153)).
FIA applies per-instrument rules, most of which set constants and some of which find partner runs by title (below).
The SANS interface has no lookup: a row may name its own settings file, which then replaces most of the GUI's settings ([gui_state_director.py L32-L39](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/python/mantidqtinterfaces/mantidqtinterfaces/sans_isis/gui_logic/presenter/gui_state_director.py#L32-L39)).
Mantid's one generic batch table, the DataProcessor widget, was used only by reflectometry ([v6.4.0 guide](https://github.com/mantidproject/mantid/blob/v6.4.0/docs/source/interfaces/framework/DataProcessorWidget.rst?plain=1#L19-L21)) and was deleted in 2022 ([mantid#34599](https://github.com/mantidproject/mantid/pull/34599)).
For us: per-run settings chosen from run metadata are common, but each technique chose its own criteria and its own table, which supports scipp/essapps#33.

**Nobody waits for a series to be complete.**
In autoprocessing, a new run with the same title and angle as a row is summed into that row, and one at another angle becomes a new row of the group ([ReductionJobs.cpp L146-L158](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/ReductionJobs.cpp#L146-L158)).
Either change resets the group, so it is stitched again ([Group.cpp L195-L205](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/Reduction/Group.cpp#L195-L205)); only the changed row is reduced again.
FIA's INTER rule attaches every file in the run's directory with the same title prefix to the new run's job ([inter_rules.py L32-L44](https://github.com/fiaisis/run-detection/blob/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397/rundetection/rules/inter_rules.py#L32-L44)), but FIA's INTER script only raises "script not implemented" ([INTER/reduce.py](https://github.com/fiaisis/autoreduction-scripts/blob/de9e1bfd96e67a6b216d7ecd8e8e3f0b868a7b5b/INTER/reduce.py)).
For TOSCA, OSIRIS and IRIS, a rule walks back over consecutive run numbers with similar titles and adds a summed job per arrival ([common_rules.py L69-L127](https://github.com/fiaisis/run-detection/blob/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397/rundetection/rules/common_rules.py#L69-L127)).
For SANS, a scatter run looks for its transmission run by title and a transmission run for its scatter run, so whichever arrives last starts the job ([sans_rules.py L275-L315](https://github.com/fiaisis/run-detection/blob/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397/rundetection/rules/sans_rules.py#L275-L315)).
For us: `Rule(series=)` reduces every run of the series at each arrival, as FIA's INTER rule would (scipp/essapps#24).
Mantid's cost per arrival is one row reduction plus one stitch; with our core that is one record per angle and a stitch request that references the persisted per-angle outputs.
Whether ESS automatic reduction should stitch as angles arrive is Open ([reflectometry](../../requirements/reflectometry.md#open)).

**Role and group come from run titles.**
Reflectometry reads the angle and group from `th=` in the title, and FIA's SANS rules read the role from a `_SANS` or `_TRANS` suffix ([sans_rules.py L87-L88](https://github.com/fiaisis/run-detection/blob/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397/rundetection/rules/sans_rules.py#L87-L88)); users type these titles at the instrument.
For us: how ESS files will say what role a run plays is the Open question that blocks the design ([requirements](../../requirements/README.md#questions-that-block-the-design)).

**A settings change resets the whole table.**
Any change in the settings tabs resets every row and group ([RunsTablePresenter.cpp L296-L300](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/RunsTable/RunsTablePresenter.cpp#L296-L300)), and the next Process or autoprocessing resume reduces all of them again, whether the change affected them or not.
For us: a record never changes, and a changed template affects only requests made from it later; reducing old runs again is a controller's explicit submission.

**Exclusions carry a reason.**
A user can type an exclude reason next to a run in the search results; excluded runs are not transferred to the table, and the reasons save with the batch ([QtSearchModel.cpp L180-L181](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Runs/QtSearchModel.cpp#L180-L181), [RunsPresenter.cpp L492](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/qt/scientific_interfaces/ISISReflectometry/GUI/Runs/RunsPresenter.cpp#L492)).
For us: the trigger loop has no exclusions; under scipp/essapps#33 they would be configuration of the application's rule.

**Provenance is a script, not a structured request.**
The reflectometry interface removed its notebook export in Mantid 4.1 "as this was not used and not useful" ([release notes](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/docs/source/release/v4.1.0/reflectometry.rst?plain=1#L164)); since 6.10 the ORSO output file embeds the reduction script ([release notes](https://github.com/mantidproject/mantid/blob/0a137707b24563d2e5d690951651fcee052ce635/docs/source/release/v6.10.0/reflectometry.rst?plain=1#L13)).
A FIA job stores the script text it ran, the commit it came from, the injected values, the container image, its state, outputs and stack trace ([models.py L77-L119](https://github.com/fiaisis/FIA-API/blob/9707aa094c4e4a9c96f31142e8777f2e0c430273/fia_api/core/models.py#L77-L119)).
A FIA rerun takes a script text and an image from the user and records no input values ([job_maker.py L96-L131](https://github.com/fiaisis/FIA-API/blob/9707aa094c4e4a9c96f31142e8777f2e0c430273/fia_api/core/job_maker.py#L96-L131)).
For us: the record holds the request; the container image is a precedent for software versions in provenance, not yet implemented ([README](../README.md#provenance-and-publication)).

**A rule edited in place rewrote history.**
In ISIS autoreduction, a run pointed at the database row of values that filled it ([records.py L170-L188](https://github.com/autoreduction/queue-processor/blob/4eab9dea17df1fe599a5fcf23a77ea1e2554de2e/autoreduce_qp/model/database/records.py#L170-L188)), and editing the values "from run N onward" overwrote that row ([configure_new_runs.py L49-L60](https://github.com/autoreduction/frontend/blob/917b665c06c91d9bfc3733c89fcbf74fa0937413/autoreduce_frontend/reduction_viewer/views/configure_new_runs.py#L49-L60)), so old runs then showed values they never used.
For us: a record holds every parameter value and does not name the rule or template it came from ([automatic-reduction.md](../automatic-reduction.md#rules)).

**FIA keeps rules as code and their configuration as data.**
Each instrument has a JSON *specification* in FIA's database; each key names a Python rule class, and its value configures it ([specifications.py](https://github.com/fiaisis/run-detection/blob/881dacdc3c07d5ee1cbf12d49843d5a8fe7fe397/rundetection/specifications.py), [models.py L136](https://github.com/fiaisis/FIA-API/blob/9707aa094c4e4a9c96f31142e8777f2e0c430273/fia_api/core/models.py#L136)).
An API call replaces the specification ([instrument_specs.py L38-L46](https://github.com/fiaisis/FIA-API/blob/9707aa094c4e4a9c96f31142e8777f2e0c430273/fia_api/routers/instrument_specs.py#L38-L46)), and run detection fetches it from the API.
FIA has working scripts for ENGIN-X, GEM, IMAT, INES, IRIS, LOQ, MARI, OSIRIS, SANS2D, TOSCA and VESUVIO; those for the reflectometers INTER, POLREF and SURF are placeholders ([autoreduction-scripts](https://github.com/fiaisis/autoreduction-scripts/tree/de9e1bfd96e67a6b216d7ecd8e8e3f0b868a7b5b)).
It learns of new runs from each instrument's last-run file, not from the catalogue, and after a restart it submits the runs between the last one it recorded and the current one ([lastrun_file_monitor.py L263](https://github.com/fiaisis/file-watcher/blob/bb9101322fc485b15cc16e448e705ec42f614ac0/file_watcher/lastrun_file_monitor.py#L263)).
For us: this is the shape scipp/essapps#33 proposes, rule functions in the application with their configuration as data, and one answer to its Open question: FIA stores the configuration behind an API so that it changes without a deployment.

## Borrow

- In the application: an ordered lookup by metadata with one fallback, duplicates refused when the table is saved, and the entry used shown per row.
- Computed values kept apart from typed ones, and shown as computed.
- Exclusions with a reason, saved with the configuration.
- Per arrival in a series, reduce only the new run and redo only the combining step.
- The container image or environment in provenance.

## Do not borrow

- Listing a run before its file can be read.
- Resetting results when settings change.
- A free `key=value` options column: it bypasses the params model's validation.
- A record that points at mutable configuration.
- A generic batch-table framework for all techniques.
