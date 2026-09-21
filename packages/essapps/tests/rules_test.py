# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates, lookups, and rules: the stored data.

See docs/developer/rules.md.
"""

from pathlib import Path

import pytest

from ess.apps.examples import LOAD
from ess.apps.rules import Between, Like, Lookup, LookupEntry, Near, Template
from ess.apps.sources import Dataset

# Templates


def test_saving_a_request_blanks_its_data_fields(template: Template) -> None:
    assert template.blanks == ('run',)
    assert template.params == {'scale': 2.0}
    with pytest.raises(ValueError, match="needs \\['run'\\]"):
        template.fill()


def test_revising_makes_a_new_version_by_copy(template: Template) -> None:
    revised = template.revise(scale=3.0)
    assert revised.version == 2
    assert revised.derived_from == 'load-defaults/v1'
    assert template.params == {'scale': 2.0}
    assert revised.params == {'scale': 3.0}


def test_a_template_with_two_blanks_names_the_field_a_dataset_fills() -> None:
    two = Template(name='two', spec=LOAD.id, blanks=('run', 'other'))
    with pytest.raises(ValueError, match='name the one a dataset fills'):
        two.field_for_dataset()
    assert two.model_copy(update={'dataset_field': 'run'}).field_for_dataset() == 'run'


# Lookup


def dataset(**metadata: object) -> Dataset:
    return Dataset(path=Path('x.h5'), pid='pid/x', metadata=dict(metadata))


def test_lookup_matches_a_value_within_a_tolerance() -> None:
    lookup = Lookup(
        name='angles',
        entries=(
            LookupEntry(
                name='low', match={'angle': Near(value=0.5, tolerance=0.2)}, fills={}
            ),
        ),
    )
    assert lookup.entry(dataset(angle=0.65)).name == 'low'
    assert lookup.entry(dataset(angle=0.7)).name == 'low'
    assert lookup.entry(dataset(angle=0.75)) is None
    assert lookup.entry(dataset(sample='a')) is None


def test_lookup_matches_a_pattern_and_a_run_range() -> None:
    lookup = Lookup(
        name='samples',
        entries=(
            LookupEntry(name='vanadium', match={'sample': Like(pattern='van*')}),
            LookupEntry(name='late', match={'run': Between(low=100)}),
            LookupEntry(name='early', match={'run': Between(high=9)}),
        ),
    )
    assert lookup.entry(dataset(sample='vanadium-rod')).name == 'vanadium'
    assert lookup.entry(Dataset(path=Path('a'), instrument='d', run=101)).name == 'late'
    assert lookup.entry(Dataset(path=Path('a'), instrument='d', run=5)).name == 'early'
    assert lookup.entry(Dataset(path=Path('a'), instrument='d', run=50)) is None


def test_the_wildcard_entry_takes_what_nothing_else_matched() -> None:
    lookup = Lookup(
        name='samples',
        entries=(
            LookupEntry(name='vanadium', match={'sample': Like(pattern='van*')}),
            LookupEntry(name='rest', fills={'scale': 9.0}),
        ),
    )
    assert lookup.entry(dataset(sample='vanadium')).name == 'vanadium'
    assert lookup.entry(dataset(sample='sio2')).fills == {'scale': 9.0}
    with pytest.raises(ValueError, match='wildcard entries'):
        Lookup(
            name='two',
            entries=(LookupEntry(name='a'), LookupEntry(name='b')),
        )


def test_a_dataset_matching_two_entries_is_an_error() -> None:
    lookup = Lookup(
        name='samples',
        entries=(
            LookupEntry(name='by-name', match={'sample': Like(pattern='van*')}),
            LookupEntry(name='by-run', match={'run': Between(low=1)}),
        ),
    )
    with pytest.raises(ValueError, match="entries \\['by-name', 'by-run'\\]"):
        lookup.entry(
            Dataset(path=Path('a'), instrument='d', run=7, metadata={'sample': 'van'})
        )
