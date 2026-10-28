# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from enum import Enum
from pathlib import Path
from typing import Any

import pydantic
import pytest

from ess.spec.parameters import (
    DatasetModel,
    Quantity,
    Scale,
    WavelengthEdges,
    WavelengthRange,
    WavelengthUnit,
)


class MaskFile(DatasetModel, kind='mask', instrument='loki'):
    pass


class Kind(Enum):
    MASK = 'mask'


class TestDatasetModel:
    def test_a_dataset_model_without_keywords_takes_raw_datasets(self) -> None:
        class RunFile(DatasetModel):
            pass

        assert DatasetModel.metadata == {'kind': 'raw'}
        assert RunFile.metadata == {'kind': 'raw'}

    def test_a_dataset_model_holds_its_class_keywords_as_metadata(self) -> None:
        assert MaskFile.metadata == {'kind': 'mask', 'instrument': 'loki'}
        assert MaskFile(path='mask.xml').model_dump() == {'path': Path('mask.xml')}

    def test_the_keywords_of_a_subclass_are_merged_over_its_parents(self) -> None:
        class BeamStopMask(MaskFile, kind='beam-stop', bank=1):
            pass

        assert BeamStopMask.metadata == {
            'kind': 'beam-stop',
            'instrument': 'loki',
            'bank': 1,
        }
        assert MaskFile.metadata == {'kind': 'mask', 'instrument': 'loki'}
        assert DatasetModel.metadata == {'kind': 'raw'}

    def test_a_subclass_without_keywords_has_the_metadata_of_its_parent(
        self,
    ) -> None:
        class OtherMask(MaskFile):
            pass

        assert OtherMask.metadata == MaskFile.metadata

    @pytest.mark.parametrize('value', ['loki', 1, 0.5, True])
    def test_a_metadata_value_is_a_string_a_number_or_a_bool(self, value: Any) -> None:
        class File(DatasetModel, kind='mask', key=value):
            pass

        assert File.metadata == {'kind': 'mask', 'key': value}

    @pytest.mark.parametrize('value', [Kind.MASK, Path('mask'), ['mask'], None])
    def test_a_metadata_value_of_another_type_is_refused(self, value: Any) -> None:
        with pytest.raises(TypeError, match='File: the metadata of a dataset model'):

            class File(DatasetModel, kind=value):
                pass

    def test_a_pydantic_config_keyword_configures_the_model_and_is_no_metadata(
        self,
    ) -> None:
        class FrozenMask(DatasetModel, frozen=True, kind='mask'):
            pass

        assert FrozenMask.metadata == {'kind': 'mask'}
        mask = FrozenMask(path='mask.xml')
        with pytest.raises(pydantic.ValidationError, match='frozen'):
            mask.path = Path('other.xml')


class TestQuantity:
    def test_scalar_and_vector(self) -> None:
        assert Quantity(value=1.0, unit='m').value == 1.0
        assert Quantity(value=(0.1, 0.2), unit='m').value == (0.1, 0.2)
        assert Quantity(value=2.0).unit is None

    def test_roundtrips_through_json(self) -> None:
        q = Quantity(value=(0.1, 0.2), unit='m')
        assert Quantity.model_validate_json(q.model_dump_json()) == q


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
