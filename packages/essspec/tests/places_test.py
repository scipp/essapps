# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path
from typing import Annotated, Literal

import pytest
import scipp as sc
from annotated_types import Ge, Gt, MinLen
from pydantic import BaseModel, Field

from ess.spec.places import File, Rows, Value, Ways, place


class Mask(BaseModel, arbitrary_types_allowed=True):
    mask: sc.DataArray


class Sample(BaseModel):
    file: Path
    thickness: float = Field(1.0, gt=0)
    runs: list[Path] = []


class Reduction(BaseModel):
    bins: int = Field(50, ge=1)
    edges: list[float] | None = None
    scales: dict[str, float] = {}
    limits: Annotated[float, Gt(0)] | None = Field(None, ge=1)
    angles: list[float] = []


class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find']
    file: Path


class Params(BaseModel):
    mask: Mask
    sample: Sample
    reduction: Reduction | None = None
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    centres: list[Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]]
    samples: list[Sample] = Field(min_length=2)


def test_a_file_takes_a_dataset() -> None:
    assert place(Sample, 'file') == File(Path, none=False)


def test_a_value_is_written_inline_whole_with_its_constraints() -> None:
    assert place(Reduction, 'bins') == Value(int, (Ge(1),), none=False)
    assert place(Reduction, 'scales') == Value(dict[str, float], (), none=False)


def test_a_list_that_may_be_none_is_a_value() -> None:
    assert place(Reduction, 'edges') == Value(list[float], (), none=True)


def test_a_value_has_the_constraints_of_its_field_then_of_its_type() -> None:
    assert place(Reduction, 'limits') == Value(float, (Ge(1), Gt(0)), none=True)


def test_a_group_with_a_data_value_takes_a_reference_only() -> None:
    assert place(Params, 'mask') == Ways(
        tag=None, inline={}, record=Mask, none=False, metadata=()
    )


def test_a_group_with_a_file_is_written_inline_only() -> None:
    assert place(Params, 'sample') == Ways(
        tag=None, inline={None: Sample}, record=None, none=False, metadata=()
    )


def test_a_plain_group_is_written_inline_or_by_reference() -> None:
    assert place(Params, 'reduction') == Ways(
        tag=None, inline={None: Reduction}, record=Reduction, none=True, metadata=()
    )


def test_a_union_is_written_inline_with_the_tag_of_an_alternative() -> None:
    described = place(Params, 'beam_centre')
    assert isinstance(described, Ways)
    assert described.tag == 'kind'
    assert described.inline == {'given': GivenCentre, 'find': FindCentre}
    assert described.record is None
    assert not described.none


def test_a_union_typed_x_or_none_takes_none() -> None:
    class MaybeCentre(BaseModel):
        beam_centre: (
            Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] | None
        ) = None

    described = place(MaybeCentre, 'beam_centre')
    assert isinstance(described, Ways)
    assert described.inline == {'given': GivenCentre, 'find': FindCentre}
    assert described.none


def test_a_list_parameter_describes_its_rows() -> None:
    assert place(Params, 'samples') == Rows(
        element=Ways(
            tag=None, inline={None: Sample}, record=None, none=False, metadata=()
        ),
        metadata=(MinLen(2),),
    )
    centres = place(Params, 'centres')
    assert isinstance(centres, Rows)
    assert isinstance(centres.element, Ways)
    assert centres.element.inline == {'given': GivenCentre, 'find': FindCentre}


def test_a_list_field_of_a_group_describes_its_elements() -> None:
    assert place(Sample, 'runs') == Rows(File(Path, none=False), ())
    assert place(Reduction, 'angles') == Rows(Value(float, (), none=False), ())


def test_a_union_without_a_discriminator_is_refused() -> None:
    class Undiscriminated(BaseModel):
        beam_centre: GivenCentre | FindCentre

    with pytest.raises(ValueError, match='discriminator'):
        place(Undiscriminated, 'beam_centre')
