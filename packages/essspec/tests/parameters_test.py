# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import pydantic
import pytest

from ess.spec.parameters import (
    File,
    Scale,
    WavelengthEdges,
    WavelengthRange,
    WavelengthUnit,
)

MaskFile = Annotated[Path, File(kind='mask', instrument='loki')]


class Masked(pydantic.BaseModel):
    mask: MaskFile
    masks: list[MaskFile] = []


class Kind(Enum):
    MASK = 'mask'


STORED = {'@ref': 'file', 'file': 'run:loki/1'}


class TestFile:
    def test_a_file_field_without_metadata_takes_raw_files(self) -> None:
        assert File().metadata == {'kind': 'raw'}

    def test_the_marker_holds_its_keywords_as_metadata(self) -> None:
        marker = File(kind='mask', instrument='loki')
        assert marker.metadata == {'kind': 'mask', 'instrument': 'loki'}
        assert repr(marker) == "File(kind='mask', instrument='loki')"

    def test_two_markers_are_equal_and_hash_equal_by_their_metadata(self) -> None:
        assert File(kind='mask', bank=1) == File(bank=1, kind='mask')
        assert hash(File(kind='mask', bank=1)) == hash(File(bank=1, kind='mask'))
        assert File() == File(kind='raw')
        assert File(kind='mask') != File()

    def test_the_metadata_cannot_be_changed_through_the_marker(self) -> None:
        marker = File(kind='mask')
        marker.metadata['kind'] = 'raw'
        assert marker.metadata == {'kind': 'mask'}

    @pytest.mark.parametrize('value', ['loki', 1, 0.5, True])
    def test_a_metadata_value_is_a_string_a_number_or_a_bool(self, value: Any) -> None:
        assert File(kind='mask', key=value).metadata == {'kind': 'mask', 'key': value}

    @pytest.mark.parametrize('value', [Kind.MASK, Path('mask'), ['mask'], None])
    def test_a_metadata_value_of_another_type_is_refused(self, value: Any) -> None:
        with pytest.raises(TypeError, match='the metadata of a file field'):
            File(kind=value)

    def test_without_paths_a_file_field_takes_a_path(self) -> None:
        masked = Masked(mask='/data/mask.xml', masks=[Path('/data/other.xml')])
        assert masked.mask == Path('/data/mask.xml')
        assert masked.masks == [Path('/data/other.xml')]
        with pytest.raises(pydantic.ValidationError, match='mask'):
            Masked.model_validate({'mask': STORED})

    def test_the_context_paths_replaces_a_file_by_its_local_path(self) -> None:
        paths = {'run:loki/1': Path('/data/1.xml')}
        masked = Masked.model_validate_json(
            json.dumps({'mask': STORED, 'masks': [STORED]}), context={'paths': paths}
        )
        assert masked.mask == Path('/data/1.xml')
        assert masked.masks == [Path('/data/1.xml')]

    @pytest.mark.parametrize(
        ('given', 'msg'),
        [
            ('/data/1.xml', 'is not a file, as the field is a file field'),
            ({'@ref': 'file', 'file': 'run:loki/2'}, 'run:loki/2 has no local path'),
        ],
    )
    def test_with_paths_a_file_field_takes_only_a_file_it_has_a_path_of(
        self, given: Any, msg: str
    ) -> None:
        paths = {'run:loki/1': Path('/data/1.xml')}
        with pytest.raises(pydantic.ValidationError, match=msg):
            Masked.model_validate_json(
                json.dumps({'mask': given}), context={'paths': paths}
            )


class TestRangeModel:
    def test_valid_range(self) -> None:
        r = WavelengthRange(start=1.0, stop=2.0)
        assert r.unit == WavelengthUnit.ANGSTROM

    def test_stop_must_exceed_start(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthRange(start=2.0, stop=1.0)
        with pytest.raises(pydantic.ValidationError):
            WavelengthRange(start=1.0, stop=1.0)

    def test_bounds_are_required(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthRange(stop=2.0)

    def test_unit_is_constrained(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthRange(start=1.0, stop=2.0, unit='m')


class TestEdgesModel:
    def test_valid_edges(self) -> None:
        edges = WavelengthEdges(start=1.0, stop=10.0, num_bins=100)
        assert edges.scale == Scale.LINEAR

    def test_stop_must_exceed_start(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthEdges(start=10.0, stop=1.0, num_bins=100)

    def test_log_scale_requires_positive_start(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthEdges(start=0.0, stop=10.0, num_bins=100, scale=Scale.LOG)
        WavelengthEdges(start=0.1, stop=10.0, num_bins=100, scale=Scale.LOG)

    def test_num_bins_bounds(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WavelengthEdges(start=1.0, stop=10.0, num_bins=0)
        with pytest.raises(pydantic.ValidationError):
            WavelengthEdges(start=1.0, stop=10.0, num_bins=10001)


class TestJsonSchema:
    def test_unit_choices_appear_as_enum(self) -> None:
        schema = WavelengthEdges.model_json_schema()
        unit_ref = schema['properties']['unit']
        enum = schema['$defs']['WavelengthUnit']['enum']
        assert set(enum) == {'Å', 'nm'}
        assert unit_ref is not None

    def test_validated_model_roundtrips_through_json(self) -> None:
        edges = WavelengthEdges(start=1.0, stop=10.0, num_bins=100, scale=Scale.LOG)
        restored = WavelengthEdges.model_validate_json(edges.model_dump_json())
        assert restored == edges
