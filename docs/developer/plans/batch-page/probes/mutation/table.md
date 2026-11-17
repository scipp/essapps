Outcomes of `mutate.py` on `tests/batch`. W15, W16, W17, W20, W23, W24, W25, W43, W52 and W53 were run at commit e92ceb0; the others at commit c79f4e5, and all of them still apply at e92ceb0 (`mutate.py --check`). "survived (+browser)" means it also survived with the browser tests.

| id | file | mutation | result | killed by (first of n) |
|---|---|---|---|---|
| S1 | state.py | RowState.text: a tweak not compared reads outdated too | killed | state_test:test_a_completed_row_not_compared_reads_outdated_but_a_tweak_reads_completed (1) |
| S2 | state.py | problems: a value at a name that is not a column is no problem | killed | definition_test:test_an_edit_at_a_column_that_is_gone_makes_its_row_incomplete (4) |
| S3 | state.py | problems: an empty cell with a default is a problem | killed | auto_test:test_a_cell_that_names_a_file_by_run_is_reduced_once (132) |
| S4 | state.py | problems: a cell reading a cancelled record is no problem | survived |  |
| S5 | state.py | results: the oldest record is the result | killed | auto_test:test_a_record_of_another_batch_on_the_label_is_replaced_if_it_differs_at_at (7) |
| S6 | state.py | differs: a value lacking on one side equals None | killed | auto_test:test_a_result_of_another_spec_counts_at_the_columns_it_holds[at0-reduced0] (2) |
| S7 | state.py | row_states: a tweak is outdated at any change | killed | reduction_test:test_a_tweak_is_outdated_once_a_cell_is_edited (6) |
| S8 | state.py | row_states: a result of another spec is not outdated by itself | killed | reduction_test:test_a_result_of_another_spec_version_is_the_rows_result_and_outdated (2) |
| S9 | state.py | ready: a running row is ready | killed | auto_test:test_a_pending_row_is_reduced_once_its_record_finishes_if_a_file_arrived (4) |
| S10 | state.py | ready: a row with problems is ready | killed | auto_test:test_a_cell_the_workspace_does_not_accept_makes_its_row_wait (38) |
| S11 | state.py | remaining: failed/cancelled rows are not left | killed | frames_test:test_remaining_leaves_out_the_rows_completed (5) |
| S12 | state.py | remaining: outdated rows are not left | killed | files_test:test_an_edit_of_an_app_is_reduced_by_the_command_given_no_files (13) |
| S13 | state.py | earlier_settings: a row changed at a column counts as earlier settings | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[completed-changed1-other1-False-True] (3) |
| S14 | state.py | earlier_settings: a result of another spec counts | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[completed-changed2-other2-False-True] (2) |
| S15 | state.py | earlier_settings: a tweak counts | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[completed-changed3-other3-False-False] (2) |
| S16 | state.py | earlier_settings: a failed result counts | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[failed-changed5-other5-False-True] (2) |
| S17 | state.py | earlier_settings: an unchanged result counts | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[completed-changed4-other4-False-False] (6) |
| S18 | state.py | arrived: a row with no result does not arrive | killed | auto_test:test_a_cell_that_names_a_file_by_run_is_reduced_once (31) |
| S19 | state.py | arrived: any change (settings too) arrives | killed | auto_test:test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated (10) |
| S20 | state.py | arrived: rows not ready (running, incomplete) arrive | killed | auto_test:test_a_cell_the_workspace_does_not_accept_makes_its_row_wait (9) |
| S21 | state.py | left_to_reduce: equals remaining | killed | state_test:test_a_result_that_differs_only_at_settings_is_not_left_to_reduce[completed-changed0-other0-True-False] (4) |
| S22 | state.py | _compared: rows not compared are not refused | killed | state_test:test_the_rules_that_read_what_changed_refuse_rows_not_compared[<lambda>0] (4) |
| A2 | auto.py | _reduce: a refusal naming no row records no reason | killed | auto_test:test_a_refusal_that_names_no_row_refuses_every_row_of_the_step (1) |
| A3 | auto.py | _reduce: each row gets the whole error, not its own reasons | killed | auto_test:test_refusals_that_only_submit_finds_are_tried_again_at_every_step (1) |
| A4 | auto.py | reduce: refused settings give no reasons | killed | auto_test:test_refused_settings_refuse_every_ready_row_until_they_change (3) |
| A5 | auto.py | reduce: refused settings keep the reasons of the step before | killed | auto_test:test_refused_settings_refuse_every_ready_row_until_they_change (3) |
| A6 | auto.py | reduce: refused never updated | killed | auto_test:test_a_refusal_that_names_no_row_refuses_every_row_of_the_step (3) |
| A8 | auto.py | _reduce: no retry after dropping the rows named | killed | auto_test:test_refusals_that_only_submit_finds_are_tried_again_at_every_step (1) |
| A9 | auto.py | reduce: at ignored, all columns | killed | auto_test:test_a_result_of_another_spec_counts_at_the_columns_it_holds[at0-reduced0] (5) |
| A10 | auto.py | __init__: at with unknown columns accepted | killed | auto_test:test_every_element_of_at_must_be_a_column[at0] (2) |
| R1 | reduction.py | _compare: cells of a row with a result are never compared | killed | auto_test:test_a_failed_row_is_reduced_again_only_for_a_change_at_at (24) |
| R2 | reduction.py | _compare: rows with empty required cells are resolved too | killed | reduction_test:test_a_cleared_cell_is_one_problem (1) |
| R3 | reduction.py | _compare: settings are not compared | killed | auto_test:test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated (16) |
| R4 | reduction.py | _typed: cache key without the type | killed | reduction_test:test_a_value_kept_is_not_taken_for_an_equal_one_of_another_type[count-1-1.0] (4) |
| R5 | reduction.py | _kept: cache key without the column | killed | reduction_test:test_a_value_kept_at_one_column_is_not_taken_at_another (1) |
| R6 | reduction.py | scan: cache not dropped | killed | reduction_test:test_a_batch_reads_a_file_by_run_as_a_new_one_does_after_a_scan (1) |
| R7 | reduction.py | _resolve_cells: nothing cached | killed | reduction_test:test_a_batch_reads_a_file_by_run_as_a_new_one_does_after_a_scan (1) |
| R8 | reduction.py | _poll: records seen before read again | killed | reduction_test:test_an_older_record_that_ends_later_is_not_the_result (1) |
| R9 | reduction.py | _poll: records of other labels taken | killed | reduction_test:test_a_record_under_another_label_is_not_a_rows_result (1) |
| R10 | reduction.py | _submit: a failed stage is not replaced | killed | reduction_test:test_a_stage_that_failed_is_replaced_within_one_reduce (1) |
| R11 | reduction.py | _submit: the stage is replaced for a row refusal too | killed | reduction_test:test_a_row_refused_leaves_the_stage_alone (1) |
| R12 | reduction.py | reduce: a running row is submitted again | killed | reduction_test:test_a_row_running_is_refused_when_picked (1) |
| R13 | reduction.py | reduce: no check before submit (not all-or-nothing on problems/running) | killed | reduction_test:test_a_row_running_is_refused_when_picked (2) |
| R14 | reduction.py | _once: a settings reason is named per row | killed | reduction_test:test_a_reason_about_the_settings_is_named_once_without_a_key[spec1] (1) |
| R15 | reduction.py | tweak: not remembered | killed | reduction_test:test_a_row_is_tweaked_until_it_is_reduced_with_the_settings (5) |
| R16 | reduction.py | tweak: a running row can be tweaked | killed | reduction_test:test_a_row_running_is_refused_when_picked (1) |
| R17 | reduction.py | _open: stage kept when the settings change | killed | auto_test:test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated (5) |
| R18 | reduction.py | _spec_changed: unbound records not of another spec | survived |  |
| R19 | reduction.py | cancel: keys ignored | killed | reduction_test:test_cancel_leaves_other_rows_when_given_keys (1) |
| R20 | reduction.py | tweak_params: params of another spec used | killed | reduction_test:test_a_tweak_starts_from_the_rows_cells_and_its_result_or_else_the_settings (2) |
| R21 | reduction.py | _states: a tweak stays tweaked after a newer record | killed | reduction_test:test_a_row_is_tweaked_until_it_is_reduced_with_the_settings (1) |
| R22 | reduction.py | _known: one unknown record breaks all | killed | auto_test:test_refusals_that_only_submit_finds_are_tried_again_at_every_step (2) |
| R24 | reduction.py | cancel: finished records cancelled too | survived |  |
| R25 | reduction.py | rows setter: assigned before the definitions keep it | killed | definition_test:test_a_change_that_cannot_be_kept_raises_and_changes_nothing (3) |
| R26 | reduction.py | settings setter: assigned before the definitions keep it | killed | definition_test:test_a_change_that_cannot_be_kept_raises_and_changes_nothing (3) |
| T1 | table.py | put: a removed row is not brought back | killed | definition_test:test_a_table_put_brings_back_a_row_removed_and_is_kept (2) |
| T2 | table.py | put: a row added takes edits left from a row gone | survived |  |
| T3 | table.py | put: every column becomes an edit (stops following the scan) | killed | files_test:test_a_table_given_with_a_source_keeps_only_what_differs_from_the_scan (4) |
| T4 | table.py | remove: a row added is marked removed | survived |  |
| T5 | table.py | remove: edits of a removed row kept | survived |  |
| T6 | table.py | reset of a cell: empty edits entry left | killed | table_test:test_reset_of_a_cell_forgets_its_edit_alone (1) |
| T7 | table.py | reset: a removed row stays removed | killed | table_test:test_reset_forgets_the_edits_and_the_removal (3) |
| T8 | table.py | _with_edits: EMPTY shows as a value | killed | definition_test:test_a_cell_cleared_stays_empty_after_a_restart_whatever_the_scan_gives (7) |
| T9 | table.py | _rows: a scanned row of an added key is not hidden | survived |  |
| T10 | table.py | rescan: a key twice accepted | killed | table_test:test_a_key_given_twice_by_a_scan_is_refused_by_key (1) |
| T11 | table.py | add: a row added takes edits left from a row gone | killed | table_test:test_a_row_added_takes_no_edit_left_from_a_row_gone (1) |
| T12 | table.py | _with_edits: EMPTY given by a scan shows | killed | table_test:test_a_row_given_with_empty_has_that_cell_empty (1) |
| D1 | definition.py | edits_data: cleared cells not saved | killed | definition_test:test_a_batch_opened_again_has_the_edits_it_had (2) |
| D2 | definition.py | edits_of: cleared cells not loaded | killed | definition_test:test_a_batch_opened_again_has_the_edits_it_had (2) |
| D3 | definition.py | load: left temporary files not removed | killed | definition_test:test_a_file_left_by_a_write_cut_off_is_removed_when_read (1) |
| D4 | definition.py | save: written in place, not atomic | survived |  |
| D5 | definition.py | path: dots not escaped | killed | definition_test:test_a_label_is_kept_in_a_folder_of_its_own (1) |
| D6 | definition.py | path: room for the temporary suffix not reserved | survived |  |
| W1 | web/server.py | _status: a row Follow refused does not read incomplete | killed | web_follow_test:test_a_done_row_reads_outdated_while_follow_is_refused_the_settings (2) |
| W2 | web/server.py | _status: failed/cancelled overridden by outdated | killed | web_test:test_a_failed_row_stays_failed_when_the_settings_change (1) |
| W3 | web/server.py | _status: earlier-settings row reads outdated | killed | web_test:test_a_changed_setting_leaves_the_rows_done_marked_with_earlier_settings (2) |
| W4 | web/server.py | _status: failed row with problems reads failed | survived (+browser) |  |
| W5 | web/server.py | _primary: no 'Retry n failed' | killed | web_test:test_a_failed_row_stays_failed_when_the_settings_change (1) |
| W6 | web/server.py | _primary: mixed failed+cancelled reads 'Reduce n remaining' | survived (+browser) |  |
| W7 | web/server.py | _primary: no 'Reduce n outdated' | killed | web_test:test_a_row_reduced_by_another_spec_version_is_outdated (1) |
| W8 | web/server.py | _primary: again counts left rows | survived (+browser) |  |
| W9 | web/server.py | _primary: earlier count 0 | killed | web_test:test_a_changed_setting_leaves_the_rows_done_marked_with_earlier_settings (1) |
| W10 | web/server.py | _primary: running count 0 | survived (+browser) |  |
| W10b | web/server.py | _primary: no progress | killed | web_test:test_rows_running_show_progress_and_cancel_shows_as_cancelled (1) |
| W11 | web/server.py | poll: rows changed by another caller do not bump version | killed | web_test:test_a_poll_sees_a_row_that_another_caller_scanned_without_a_record (1) |
| W12 | web/server.py | poll: restart ignored | killed | web_test:test_a_poll_at_the_same_version_answers_204_but_not_after_a_restart (1) |
| W13 | web/server.py | poll: new records do not bump version | killed | web_test:test_a_poll_sees_a_reduction_finish (1) |
| W14 | web/server.py | poll: listing not refreshed | killed | web_test:test_a_poll_sees_a_row_that_another_caller_scanned_without_a_record (1) |
| W15 | web/server.py | _caught_up: new records do not count as change | killed | web_follow_test:test_a_step_after_a_result_finished_tells_the_page (1) |
| W16 | web/server.py | _caught_up: moved runs do not count | killed | web_follow_test:test_a_step_that_changes_one_thing_the_page_shows_tells_the_page[runs] (1) |
| W17 | web/server.py | _step: refusals do not count | killed | web_follow_test:test_a_step_that_changes_one_thing_the_page_shows_tells_the_page[refusals] (1) |
| W18 | web/server.py | _step: failure does not count | survived (+browser) |  |
| W19 | web/server.py | _step: rows do not count | survived (+browser) |  |
| W20 | web/server.py | _step: every step changes the version | killed | web_follow_test:test_a_step_that_finds_nothing_new_changes_no_version[correction] (3) |
| W21 | web/server.py | _step: failure sticks after success | killed | web_follow_test:test_a_step_that_raises_is_said_and_the_next_step_runs (1) |
| W22 | web/server.py | step: _rows not updated | killed | web_follow_test:test_a_step_that_finds_nothing_new_changes_no_version (1) |
| W23 | web/server.py | state: refusals by Follow shown when not following | killed | web_follow_test:test_a_row_the_step_could_not_submit_shows_why (1) |
| W24 | state.py | refused_again: all rows, not arrived | killed | state_test:test_refused_again_keeps_the_reasons_of_the_rows_a_step_submits_again (1) |
| W25 | state.py | refused_again: done rows marked refused while settings refused | killed | state_test:test_refused_again_keeps_the_reasons_of_the_rows_a_step_submits_again (2) |
| W26 | web/server.py | _action: version not bumped | survived (+browser) |  |
| W27 | web/server.py | _action: _rows not updated | survived (+browser) |  |
| W28 | web/server.py | _action: form not reset after a change not kept | killed | web_test:test_a_change_the_definitions_cannot_keep_answers_422_and_nothing_changes (1) |
| W29 | web/server.py | _show_settings: form always reset to the batch | killed | web_test:test_a_form_adds_and_removes_elements_of_a_list[add-banks0] (3) |
| W30 | web/server.py | _write_settings: written with something missing | killed | web_test:test_settings_with_something_missing_are_not_written_reduced_or_downloaded (1) |
| W31 | web/server.py | _write_settings: shown not recorded | survived |  |
| W31b | web/server.py | _write_settings: unfit kept | killed | web_test:test_settings_kept_that_the_form_cannot_show_are_named_and_kept_until_a_change (1) |
| W32 | web/server.py | _forget: all reasons of the row forgotten | survived |  |
| W33 | web/server.py | edit: refusal not forgotten | killed | web_test:test_a_refused_value_stays_marked_until_it_is_edited (1) |
| W34 | web/server.py | reduce: 'earlier' reduces left_to_reduce | killed | web_follow_test:test_a_step_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them (2) |
| W35 | web/server.py | reduce: 'remaining' reduces every ready row | killed | web_test:test_reduce_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them (1) |
| W35b | web/server.py | reduce: 'all' reduces only left_to_reduce | killed | web_test:test_reduce_all_reduces_the_rows_done_again (1) |
| W36 | web/server.py | reduce: refusals kept after success | survived (+browser) |  |
| W37 | web/server.py | _forget_settings: any setting edit forgets all setting refusals | survived |  |
| W39 | web/server.py | submit_tweak: notice when not following | survived |  |
| W41 | web/server.py | _row: result always 'current' | survived (+browser) |  |
| W43 | web/server.py | tick: steps when not following | killed | web_follow_test:test_a_tick_does_nothing_while_not_following (2) |
| W44 | web/server.py | follow: no step at once | killed | web_follow_test:test_a_done_row_reads_outdated_while_follow_is_refused_the_settings (4) |
| W46 | web/server.py | _progress: off by one | killed | web_test:test_rows_running_show_progress_and_cancel_shows_as_cancelled (1) |
| W52 | web/server.py | _scanned: runs version not bumped by a scan or a correction | killed | web_follow_test:test_a_step_that_finds_nothing_new_changes_no_version[correction] (2) |
| W53 | web/server.py | _new_runs: runs version never bumped | killed | web_follow_test:test_a_step_shows_new_rows_and_runs_and_reduces_the_rows_that_arrived (2) |
| W54 | web/server.py | use_settings: settings of another spec taken | killed | web_test:test_a_row_reduced_by_another_spec_version_is_outdated (1) |
