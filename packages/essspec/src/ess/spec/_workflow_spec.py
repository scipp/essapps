# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: the interface of a workflow, without its code.

A :class:`WorkflowSpec` names a workflow and holds two pydantic models: the
*params*, everything a request gives the workflow, and the *outputs*, the
results it returns. This docstring defines the terms of both models and gives
the reasons for their rules; :class:`WorkflowSpec` lists the rules it checks.
The other modules of ``ess.spec`` and ``ess.apps`` apply them and link here.
``packages/essspec/docs/params.md`` is a guide for workflow authors.

A spec of an I(Q) reduction, with each kind of field once::

    from ess.spec import RunFile              # types a file field: a raw file

    class WavelengthRange(BaseModel):         # a group
        start: float                          # a scalar
        stop: float

    class Reduction(BaseModel):
        bins: Annotated[int, Gt(0)] = 50      # a scalar, with a constraint
        banks: list[int] = []                 # a list of scalars; [] if empty
        q_max: float | None = None            # a scalar, or None
        range: WavelengthRange | None = None  # a group in a group, or None

    class Transmission(BaseModel, arbitrary_types_allowed=True):
        fraction: sc.DataArray                # not a field type: only a
                                              # reference to an output fills
                                              # a Transmission

    class Sample(BaseModel):                  # a row of the table samples
        run: RunFile                          # a file
        can: RunFile | None = None            # a file, or None
        thickness: float = 1.0
        transmission: Referenced[Transmission]  # a reference fills it

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    class GivenCentre(BaseModel):             # an alternative of a union
        kind: Literal['given'] = 'given'      # its tag
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):              # another alternative
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class BeamCentre(BaseModel):              # what a centre finder outputs
        x: float
        y: float

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()    # a default is an instance
        mask: Referenced[PixelMask] | None = None  # a reference, or None
        centre: (                             # written, or a reference
            Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
            | Referenced[BeamCentre]
        )
        direct_beam: RunFile                  # no default: required
        samples: list[Sample]                 # a table: no default

    class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        pixel_mask: PixelMask                 # fits the parameter mask

    IOFQ = WorkflowSpec(
        name='iofq', version=1, title='I(Q)', description='...',
        params=IofQParams, outputs=IofQOutputs, tables=('samples',),
    )

A request gives the parameters as a dict by name. In this request for
``IOFQ``, ``m`` is the record of an earlier request whose spec has the
output ``pixel_mask``, and ``t`` that of a request whose spec has the output
``transmission``, of type ``Transmission``::

    {
        'reduction': {                           # banks and q_max take
            'bins': 100,                         # their defaults
            'range': {'start': 1.0, 'stop': 9.0},  # a group in a group
        },
        'mask': m.ref('pixel_mask'),             # a reference to an output
        'centre': {'kind': 'find'},              # the tag picks FindCentre
        'direct_beam': file(run=99),             # a file
        'samples': [                             # two rows
            {'run': file(run=101), 'transmission': t.ref('transmission')},
            {
                'run': file(run=102),
                'can': file(run=103),
                'thickness': 2.0,
                'transmission': t.ref('transmission'),
            },
        ],
    }

``'centre': c.ref('beam_centre')``, with ``c`` the record of a centre
finder, fills ``centre`` with a ``BeamCentre``.

Terms
-----

*Parameter*
    A field of the params model, such as ``reduction``.
*Group*
    A pydantic model, such as ``Reduction``, but not a ``RootModel``. A
    request writes a group as a JSON object of its *fields*. A field left out
    takes its default. A parameter is a field of the params model, which is
    a group too, so the rules for a field hold for a parameter.
*List*, *element*, *table*, *row*
    A list, ``list[X]``, holds *elements*, each of type ``X``. A *table* is a
    list parameter whose elements an accumulator takes a push at a time,
    such as ``samples``. ``tables`` names the tables. An element of a table
    is a *row*.
*Union of groups*
    A pydantic discriminated union, such as ``GivenCentre | FindCentre`` in
    the type of ``centre``. Each alternative is a group with the *tag* field
    named by ``discriminator``, typed a ``Literal`` of one string. A request
    writes the fields of one alternative and its tag, which says which
    alternative it is.
*Scalar*
    An ``int``, ``float``, ``str``, ``bool``, or ``datetime`` (also
    pydantic's ``AwareDatetime`` and ``NaiveDatetime``), or an enum or a
    ``Literal`` whose values are of these types.
*File field*, *file*
    A file field is a field that a file fills, typed ``Annotated[Path,
    File(**metadata)]``, such as ``direct_beam: RunFile``. The marker
    :class:`~ess.spec.parameters.File` holds the metadata that a file must
    have to fill the field, ``kind='raw'`` unless given. A request writes a
    *file* there as ``file(...)``, such as ``file(run=99)``. The backend
    resolves it to the file's identity when the request is submitted. The
    params model, and so the binding, holds the file's local path, a
    ``Path``. ``RunFile`` *types* a file field. A *file parameter* is a
    parameter that is a file field, or a list of one.
*Reference to an output*
    ``m.ref('pixel_mask')`` names the output ``pixel_mask`` of the record
    ``m``. ``acc.ref(...)`` names an output of an accumulator. The backend
    replaces the reference by the output's data when the request runs.
``Referenced[T]``
    The type of a field that a reference to an output fills, with data that
    fit the group ``T``, such as ``mask`` and ``Sample.transmission``. It is
    ``Annotated[T, REFERENCED]`` (see :data:`~ess.spec.parameters.Referenced`):
    pydantic, a binding, and any direct use of the params model see ``T``,
    and take it inline.
*Field type*
    The type of a field, at any depth, is one of::

        int, float, str, bool, datetime, an enum, a Literal  # a scalar
        Annotated[Path, File(...)]                     # of a file field
        Reduction                                      # a group
        Annotated[A | B, Field(discriminator='kind')]  # a union of groups
        Referenced[T]                                  # T a group
        Annotated[A | B, ...] | Referenced[C]          # members of both
        RunFile | Referenced[C]                        # a file, or a
                                                       # reference
        X | None, list[X]                   # X a field type, not a list,
                                            # and not X | None

    A scalar may carry constraints, such as ``Gt(0)``. Any other type is
    refused, such as ``tuple[float, float]``, ``dict[str, float]``,
    ``int | str``, or ``sc.DataArray``. The framework does not look inside
    the ``T`` of ``Referenced[T]``, so a field of ``T`` may be of any type,
    such as ``PixelMask.mask``, but a ``Path`` or ``Referenced[...]``.
*JSON form*, *stored form*
    The JSON form of a value is what it dumps to as JSON,
    ``model_dump(mode='json')``. The record of a request stores what the
    request writes, in JSON form: the *stored form*. The stored form of a
    file is ``{'@ref': 'file', 'file': 'run:loki/99'}``, with the identity
    the backend resolved, and that of a reference to an output is
    ``{'@ref': 'output', 'record': ..., 'output': ...}`` (see
    :mod:`ess.spec.refs`).
*Position*
    Where a value sits in the params, by field names and list indices, such
    as ``reduction.range.start`` or ``samples[1].transmission``. An error
    names the position of each problem.

What a request writes
---------------------

The type of a field says what a request writes there:

- A scalar, such as ``float``: its JSON value.
- A group, such as ``Reduction``: a JSON object of its fields, each what its
  type takes.
- A union of groups, such as ``GivenCentre | FindCentre``: a JSON object of
  the fields of one alternative, with its tag.
- A file field, such as ``direct_beam: RunFile``: a file.
- ``Referenced[T]``, such as ``Referenced[PixelMask]``: a reference to an
  output whose data fit ``T``.
- A union of members written inline and members ``Referenced[...]``, such as
  the type of ``centre``, or ``BeamCentre | Referenced[BeamCentre]``, or of
  a file field and members ``Referenced[...]``, such as ``RunFile |
  Referenced[C]``: what one of its members takes. The key ``@ref`` tells a
  reference from a value written inline, and a file from a reference to an
  output; then the tag tells the alternatives apart.
- ``X | None``: what ``X`` takes, or ``None``.
- ``list[X]``: a list of elements, each what ``X`` takes.

In Python, a request may give a value as an instance of its type, a group
or the params model, if the value holds no reference to an output. pydantic
refuses a reference in an instance of a group, so a value that holds one is
given as a dict, such as each row of ``samples`` above. An instance holds a
``Path`` at each file field, which names the file by its path, as
``file(path=...)`` does. In JSON a file is written ``{'@ref': 'file',
...}``, and a string at a file field is refused.

The framework derives from the params model a *request type*, the type of
what a request writes at each field: the params type with each
``Referenced[T]`` replaced by a reference whose data fit ``T``, and each
file field by a file, at any depth (see :mod:`ess.spec.request_types`). It
validates a request, its JSON schema is the params schema of the spec (see
:meth:`WorkflowSpec.serialize`), and a form made from the params model
offers what each field takes. The params model itself validates the stored
form, with each file replaced by its local path, and, when the request
runs, each reference by its data. The rules behind it:

- Each field is of a field type, at any depth, so that a form and the
  params schema know what a request writes at every field. A group does
  not contain itself, at any depth, so that the walk through the field
  types ends.
- ``Referenced[T]`` may type any field of the params, but no output, since
  the data of an output are not a reference.
- A group is validated when the request is submitted, so that an error
  shows before anything runs, with the local path of each file it holds. A
  group that holds a reference to an output, at any depth, is validated
  when the request runs: the output may not exist when the request is
  submitted, and the group's validators see the data read. A failure then
  fails the record, or stops the accumulator for a push. A validator raises
  ``ValueError`` for a value it does not take. Which validation runs when
  is stated in :func:`~ess.spec.request_types.held`.
- A file is a value, like a scalar. The file source checks each file when
  the request is submitted: it is known, the request's proposal may read
  it, and its metadata fit the marker's. A file field carries no other
  check of a file: one that reads the file would read it from a network
  mount when the request is submitted. A check of a file's content belongs
  to the workflow, and a workflow sees the path only, never the identity.
- A reference to an output fits a field if the output's type is the ``T``
  of a ``Referenced[T]`` there, or a subclass of it (see
  :func:`~ess.spec.field_types.fits`). So ``T`` is a group: ``float`` or
  ``sc.DataArray`` would fit every output of that type, and a picker of
  records would list unrelated outputs. An array is a field of a group
  whose class says what it is, such as ``PixelMask``.
- No output holds a ``Path``, so that every file comes from the file
  source, which checks it. So ``T`` has no ``Path``, at any depth.
- A union with members ``Referenced[...]`` carries its discriminator on the
  union of the members written inline, as in ``centre``, since a reference
  to an output has no tag. A referenced group needs no tag, so the workflow
  that computes one need know nothing of the specs that take it.
- A list is ``[]`` when it has no elements, so a list is not typed
  ``list[X] | None``. Each element is a value, so the element of a list is
  not typed ``X | None``. A list is not of lists: its element is a group
  that holds a list, whose class says what each inner list is.
- A list is validated whole. A table is validated a row at a time, since an
  accumulator takes a few rows at a time. So a table carries only length
  constraints, such as ``Field(min_length=1)``, which the backend checks on
  all rows.
- The rows of a table are distinct, compared by their stored form: a file by
  its identity, a reference to an output by its record and output. Two
  references to equal data are two rows.
- A binding sets the sciline key of a parameter's class (see
  :class:`~ess.spec.pipeline.PipelineBinding`). By convention a parameter is
  a group, a union of groups, a file field, or a list of one. A group is
  one tab of a form and one key of the binding. A parameter of another
  type, such as ``bins: int`` or ``direct_beam: RunFile``, needs ``keys=``
  in the binding, such as the workflow's own key of a file's path,
  ``keys={'direct_beam': (DirectBeamFilename,)}``.
- Files that vary independently are separate parameters, not fields of one
  group. A stage fixes or blanks whole parameters, and ``sciline.Stage``
  decides what it computes again on each call by the graph alone: all that
  depends on the key of a blank. A group is one key, so a blank group of
  sample, can, and direct beam computes the can's branch again on every
  call, though only the sample changes. A group of files fits files that
  always change together, such as the two channels of a polarized run; a
  provider of ``make_pipeline`` may take the group apart into the
  workflow's keys.
- A default is a value that a request may write, as validating it gives: an
  instance of a group, not a dict, and never a reference or a file. So a
  field typed ``Referenced[T]`` has no default but ``None``, and a file
  field has no default but ``None`` or ``[]``.

What a spec refuses
-------------------

The models are ordinary pydantic models: what the framework needs, it reads
from the field types (see :mod:`ess.spec.field_types`). Making a spec checks
them. Each comment below names a rule that a spec breaks, with the fix::

    class Geometry(BaseModel):
        # A field that takes a file is a file field: file: GeometryFile, with
        # GeometryFile = Annotated[Path, File(kind='geometry')].
        file: Path

    class Calibration(BaseModel):
        # File(...) marks a Path: a request names a file, not its run. The
        # metadata of the file say the rest.
        run: Annotated[int, File(kind='calibration')]

    class Flux(BaseModel):
        run: RunFile

    class Reduction(BaseModel):
        # No aliases: a request writes a field by its name.
        bins: int = Field(10, alias='nbins')
        # No exclude: the record stores the JSON form, which would leave
        # out the value.
        width: float = Field(1.0, exclude=True)

        # No serializers or computed fields: the JSON form of a group is the
        # JSON object of its fields.
        @computed_field
        def area(self) -> float: ...

    class Binning(BaseModel):
        # A tuple is not a field type: use a group, or, if the data of an
        # output fill a Binning, type the field that holds it
        # Referenced[Binning].
        limits: tuple[float, float] = (0.0, 1.0)
        # Each element is a value: a list is not of lists. Use a list of a
        # group that holds a list.
        groups: list[list[int]] = []

    class Sample(BaseModel):
        # No NewType or type alias: a class says what a value is.
        run: RunNumber

    class Specimen(BaseModel):
        # A group does not contain itself, at any depth.
        inner: Specimen | None = None

    class FoundCentre(BaseModel):             # what a centre finder outputs
        kind: Literal['found'] = 'found'
        x: float
        y: float

    class Params(BaseModel):
        bins: int = 10
        reduction: Reduction
        binning: Binning = Binning()
        sample: Sample
        specimen: Specimen
        geometry: Geometry
        calibration: Calibration
        # T of Referenced[T] is a group: Referenced[PixelMask] | None.
        mask: Referenced[PixelMask | None] = None
        # No output holds a Path: a file is given as file(...).
        flux: Referenced[Flux]
        # A reference has no tag: the discriminator is on the union written
        # inline, Annotated[GivenCentre | FindCentre, Field(...)]
        # | Referenced[FoundCentre].
        centre: Annotated[
            GivenCentre | FindCentre | Referenced[FoundCentre],
            Field(discriminator='kind'),
        ]
        # A list without elements is [], not None.
        transmissions: list[RunFile] | None = None
        # An element is a value: the element of a list is not X | None.
        masks: list[Referenced[PixelMask] | None] = []
        # Pydantic does not validate a default, so a default is the value
        # validating it gives: WavelengthRange(start=1.0, stop=2.0).
        range: WavelengthRange = {'start': 1.0, 'stop': 2.0}
        # A table has no default.
        sample_runs: list[RunFile] = []
        # A name is of a parameter or of an output, not both.
        iofq: RunFile

        # The params model has model validators only: each parameter is
        # validated alone.
        @field_validator('bins')
        @classmethod
        def _positive(cls, bins: int) -> int: ...

    class Outputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        # An output is X, X | None, list[X], or dict[str, X], not a union.
        width: float | str
        # No output holds a Path, a file field among them: every file comes
        # from the file source.
        raw: RunFile
        # Referenced[...] marks a field of the params.
        pixel_mask: Referenced[PixelMask]

    WorkflowSpec(..., params=Params, outputs=Outputs, tables=('sample_runs',))

A check of one value belongs to the value's type, such as a field validator
of a group. A check across parameters belongs to the binding or the driver,
and fails when the request runs. The params model may state one as a model
validator, but the framework never calls it, since a stage and a driver
never get a whole params instance. A binding may run it,
``IofQParams.model_validate(dict(params))``.

A request gives every table, ``[]`` for one without rows, so that no request
leaves out a table by mistake.

Two forms
---------

* :class:`WorkflowSpec` is the in-process form. It holds the model *classes*,
  so consumers in the same process get full pydantic validation.
* :class:`SerializedWorkflowSpec` is the plain-data form produced by
  :meth:`WorkflowSpec.serialize`. It is what a service announces to remote
  consumers. There is intentionally no inverse: validators do not round-trip
  through JSON Schema, and authoritative validation always happens in the
  process owning the model classes.

Both examples are tests, in ``workflow_spec_examples_test.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path, PurePath
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Self,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

from annotated_types import BaseMetadata, GroupedMetadata, MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic.fields import FieldInfo

from . import field_types, request_types
from .parameters import REFERENCED, File
from .refs import FileRef, ParamsError, iter_refs


class NoParams(BaseModel):
    """
    Params model for workflows that take no parameters.

    Workflows always have a params model, so consumers never branch on its
    absence; "takes no parameters" is expressed as a model with no fields.
    Extra fields are rejected so that sending params to such a workflow is an
    error rather than silently ignored.
    """

    model_config = ConfigDict(extra='forbid')


class _SpecFields(BaseModel, frozen=True):
    """Metadata fields shared by both forms of the workflow spec."""

    name: str = Field(
        min_length=1,
        description=(
            "Machine-readable workflow identifier. Unique within the context "
            "that enumerates the spec; global uniqueness is the enumerator's "
            "responsibility, not the spec's."
        ),
    )
    version: int = Field(
        ge=1,
        description=(
            "Version of the workflow interface. Increment on any change a "
            "consumer could observe: params, outputs, or semantics."
        ),
    )
    title: str = Field(min_length=1, description="Display title of the workflow.")
    description: str = Field(
        min_length=1, description="Description of what the workflow computes."
    )
    code_revision: str | None = Field(
        default=None,
        description=(
            "Git commit or package version of the workflow code this spec "
            "describes, so that a record made from a development branch is "
            "honest about what ran. Provenance, not identity: the interface "
            "version is ``version``."
        ),
    )
    tables: tuple[str, ...] = Field(
        default=(),
        description=(
            "The list parameters whose rows an accumulator takes a push at a "
            "time, each a table."
        ),
    )

    def __hash__(self) -> int:
        # The schemas of the serialized form are dicts, which pydantic's hash
        # of all fields refuses; equal specs have equal identities, so these
        # suffice.
        return hash((type(self), self.name, self.version))


class WorkflowSpec[P: BaseModel, O: BaseModel](_SpecFields, frozen=True):
    """
    Implementation-independent specification of a workflow's user interface.

    Holds identity and display metadata and the pydantic model classes of the
    workflow's params and outputs. Contains no factory, no executor, and no
    reference to any workflow implementation. The module defining a spec
    must therefore be importable without importing the workflow code, so
    that a service can load and validate every spec it knows without that
    code.

    The spec is generic over its models, which a type checker infers from
    ``params=`` and ``outputs=``, so that a binding's
    ``compute(params: P) -> O`` is checked against them. Code that takes any
    spec annotates it ``WorkflowSpec[Any, Any]``.

    Making a spec checks the rules below. ``packages/essspec/docs/params.md``
    is a guide to writing a spec, and :mod:`ess.spec._workflow_spec` defines
    the terms and gives the reasons for the rules.

    Parameters
    ----------
    name:
        Machine-readable identifier of the workflow, not empty. Unique within
        the context that lists the specs.
    version:
        Version of the workflow interface, at least 1. Increase it on any
        change that a consumer can observe: the params, the outputs, or what
        the workflow computes.
    title:
        Display title of the workflow, not empty.
    description:
        What the workflow computes, not empty.
    params:
        Pydantic model class of everything a request gives the workflow. Each
        field is of a field type (see Rules). Defaults to :class:`NoParams`,
        a workflow that takes nothing.
    outputs:
        Pydantic model class of the results. The title and description of a
        field are its display metadata. The order of the fields is the order
        in which consumers present the outputs, and they may select the
        first, so put the primary output first.
    tables:
        Names of the list parameters whose rows an accumulator takes a push
        at a time, each a *table*. Defaults to none.
    code_revision:
        Git commit or package version of the workflow code, so that a record
        made from a development branch says what ran. Provenance, not
        identity: the interface version is ``version``.

    Raises
    ------
    pydantic.ValidationError
        If the spec breaks a rule. The message lists the problems found, each
        with the model and field it concerns.

    Rules
    -----
    In the rules, a *group* is a pydantic model that is not a ``RootModel``.
    A *parameter* is a field of the params model, which is a group too, so
    a rule for the fields of a group holds for the parameters.

    Field types:

    - Each field of the params model, and of each group in it at any depth,
      is of one of these types, and no other:

      - a scalar: ``int``, ``float``, ``str``, ``bool``, ``datetime`` (also
        ``AwareDatetime`` and ``NaiveDatetime``), or an enum or a ``Literal``
        whose values are ``str``, ``int``, ``float``, or ``bool``, possibly
        with constraints such as ``Gt(0)``;
      - a file field, ``Annotated[Path, File(...)]``;
      - a group;
      - a union of groups;
      - ``Referenced[T]``;
      - a union of members written inline and members ``Referenced[...]``;
      - ``X | None`` or ``list[X]``, with ``X`` a field type.

      So ``tuple``, ``set``, ``dict``, ``date``, ``bytes``, a ``RootModel``,
      an array, and ``int | str`` are refused. Data that an output fills in
      are a field of a group, ``Referenced[Group]``.
    - A group does not contain itself, at any depth.
    - A list is not typed ``list[X] | None`` (a list without elements is
      ``[]``), its element is not typed ``X | None``, and it is not a list of
      lists.
    - No type is a ``NewType`` or a type alias (``type X = ...``), at any
      depth, also inside the ``T`` of ``Referenced[T]`` and in the outputs:
      a class says what a value is. A name assigned a type, such as
      ``RunFile = Annotated[Path, File()]``, is that type.

    Defaults (see :func:`~ess.spec.request_types.validated`):

    - A default is the value that validating it gives. A group's default is
      an instance, not a dict, and an enum's is a member, not its value. It
      meets the constraints of the field.
    - A default is not a reference to an output, and names no file.
    - A file field has no default but ``None``, or ``[]`` for a list.
    - A field typed ``Referenced[T]`` has no default but ``None`` (type it
      ``Referenced[T] | None``), or ``[]`` for a list.
    - A ``default_factory`` takes no other values of the model.

    Files (see :class:`~ess.spec.parameters.File`):

    - ``File(...)`` marks a ``Path``, once. ``Annotated[int, File()]`` and
      ``Annotated[Path | None, File()]`` are refused; write
      ``Annotated[Path, File()] | None``.
    - A ``Path`` without ``File(...)`` is refused.
    - A file field takes no constraint, such as ``MinLen(2)``. A validator
      such as ``AfterValidator(...)`` is allowed: it sees the local path.
    - A file field is in a union only with members ``Referenced[...]`` and
      ``None``, since a field takes one kind of file.

    ``Referenced[T]``:

    - It types a field of the params, at any depth, but no output.
    - ``T`` is a group. ``Referenced[float]``, ``Referenced[sc.DataArray]``,
      and ``Referenced[A | B]`` are refused; write ``Referenced[A] |
      Referenced[B]``. The fields of ``T`` are of any type otherwise.
    - ``T`` holds no ``Path`` (so no file field) and no ``Referenced[...]``,
      at any depth, since no output holds either.

    Unions of groups (see :func:`~ess.spec.field_types.alternatives`):

    - The discriminator is the name of the tag field,
      ``Annotated[A | B, Field(discriminator='kind')]``, not a callable.
    - The tag field of each alternative is a ``Literal`` of one string, its
      own, without an alias.
    - A union that holds a group holds groups only, besides ``None`` and
      members ``Referenced[...]``: ``A | float`` is refused.
    - In a union with members ``Referenced[...]``, the discriminator is on
      the union of the members written inline,
      ``Annotated[A | B, Field(discriminator='kind')] | Referenced[C]``,
      since a reference to an output has no tag.

    Tables:

    - ``tables`` names a parameter of the params model, once.
    - A table is typed ``list[X]``, and has no default.
    - A table carries only length constraints, such as
      ``Field(min_length=1)``, and no other constraint or validator. The
      type of its element checks a row.

    Outputs:

    - A name is of a parameter or of an output, not both.
    - An output is typed ``X``, ``X | None``, ``list[X]``, or ``dict[str, X]``
      (also ``| None``), not a union of types. ``X`` is any type, arrays
      among them.
    - No output holds a ``Path`` at any depth, a file field among them, since
      every file comes from the file source.
    - No output holds ``Referenced[...]`` at any depth, which marks a field
      of the params.

    Params model and groups:

    - A field has no alias (``alias``, ``validation_alias``,
      ``serialization_alias``) and no ``exclude``, so that the JSON form of a
      group, which a record stores, is the object of its fields.
    - A group has no serializers and no computed fields, for the same reason.
      It may have field validators and model validators of any mode.
    - The params model has no field validators, serializers, or computed
      fields: each parameter is validated alone. It may have model
      validators, which the framework never calls; a binding may,
      ``IofQParams.model_validate(dict(params))``.
    """

    # A type checker cannot check a default against P, which only params=
    # decides; pyright honours this mypy ignore too.
    params: type[P] = Field(  # type: ignore[assignment]
        default=NoParams,
        description=(
            "Pydantic model class of everything the workflow takes, each "
            "field of a field type. Defaults to :class:`NoParams`."
        ),
    )
    outputs: type[O] = Field(
        description=(
            "Pydantic model class of the workflow's results. Field title and "
            "description are the display metadata; a field may be optional "
            "when the workflow does not always produce it. Order is "
            "meaningful: consumers present outputs in this order and may "
            "auto-select the first, so put the primary output first."
        ),
    )

    @model_validator(mode='after')
    def _check_models(self) -> Self:
        problems = [
            *_distinct(self.params, self.outputs),
            *_params_problems(self.params),
            *_outputs_problems(self.outputs),
            *_tables_problems(self),
        ]
        if problems:
            raise ValueError(f'{self.name}: {"; ".join(problems)}')
        return self

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema gives each field, at any depth, the sources a
        request may write there (see the module docstring).
        :class:`SerializedWorkflowSpec` shows the schema of each source. The
        projection is one-way: pydantic validators do not survive it, so a
        consumer of the serialized form can validate only optimistically.
        """
        return SerializedWorkflowSpec(
            name=self.name,
            version=self.version,
            title=self.title,
            description=self.description,
            code_revision=self.code_revision,
            tables=self.tables,
            params_schema=_params_schema(self),
            outputs_schema=_outputs_schema(self.outputs),
        )


class SerializedWorkflowSpec(_SpecFields, frozen=True):
    """
    Plain-data form of a workflow spec, safe to send across process boundaries.

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON.
    ``params_schema`` is a JSON schema of the params. At each field, at any
    depth, it allows the sources that a request may write there (see
    :mod:`ess.spec._workflow_spec`). A UI made from it picks a widget by
    these sources:

    ==================================  ===================================
    Schema at a field                   Widget
    ==================================  ===================================
    a scalar                            an input of its type
    a file                              a picker of files (a file field)
    the object of a group's fields      the group's fields
    ``oneOf``, with ``discriminator``   a choice of the alternatives of a
                                        union, each with its fields
    a reference to an output only       a picker of records
                                        (``Referenced[T]``)
    one of the first three, and a       its widget, and a choice to pick a
    reference to an output              record instead
    ``null`` in ``anyOf``               a toggle: ``X | None``
    an array                            a list of elements
    ==================================  ===================================

    The params schema is the JSON schema of the spec's request type (see
    :mod:`ess.spec.request_types`), which also validates a request, so the
    two agree. The sources a field takes are its ``oneOf``. The schema of a
    reference to an output is the ``@ref`` object of an output (see
    :mod:`ess.spec.refs`), with the name of the type it must fit under
    ``dataType``, as the outputs schema names the type of each output. A
    file is the ``@ref`` object of a file, with the marker's metadata under
    ``metadata``, which say which files fit. The schemas of the parameter
    ``reduction``, a parameter ``centre`` typed ``BeamCentre |
    Referenced[BeamCentre]``, a file field ``mask`` typed ``MaskFile``,
    the definition of a
    file, and the field ``Reduction.bins``::

        "reduction": {"$ref": "#/$defs/Reduction", ...}
        "centre":    {"oneOf": [{"$ref": "#/$defs/BeamCentre", ...},
                                {"$ref": "#/$defs/OutputRef",
                                 "dataType": "BeamCentre", ...}]}
        "mask":      {"$ref": "#/$defs/FileRef", "metadata": {"kind": "mask"}}
        "FileRef":   {"properties": {"@ref": {"const": "file"}, "file": ...}}
        "bins":      {"type": "integer", "default": 10}
    """

    params_schema: dict[str, Any] = Field(
        description="Schema of the workflow's params."
    )
    outputs_schema: dict[str, Any] = Field(
        description="Schema of the workflow's outputs, by the type of each value."
    )


def _distinct(params: type[BaseModel], outputs: type[BaseModel]) -> list[str]:
    """Refuse a name of both a parameter and an output."""
    return [
        f'{name}: a field of both {params.__name__} and {outputs.__name__}'
        for name in params.model_fields.keys() & outputs.model_fields.keys()
    ]


def _decorators(model: type[BaseModel], kinds: Sequence[str]) -> dict[str, list[str]]:
    """The decorators of ``model``, and of its bases, of each of ``kinds``, by kind."""
    decorators = model.__pydantic_decorators__
    return {
        kind: sorted(getattr(decorators, kind))
        for kind in kinds
        if getattr(decorators, kind)
    }


def _model_validators_only(model: type[BaseModel]) -> list[str]:
    """
    Refuse a decorator of the params model itself but a model validator: a
    field validator, a field or model serializer, or a computed field.
    """
    kinds = ('validators', 'field_validators', *_SERIALIZERS)
    if found := _decorators(model, kinds):
        return [
            f'{model.__name__}: no decorators but model validators, {found}; '
            "the check or the JSON form of one value belongs to the value's type"
        ]
    return []


_SERIALIZERS = ('field_serializers', 'model_serializers', 'computed_fields')


def _params_problems(model: type[BaseModel]) -> list[str]:
    problems = [
        *_model_validators_only(model),
        *_fields_problems(model, (model,)),
    ]
    return list(dict.fromkeys(problems))


def _fields_problems(
    model: type[BaseModel], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """
    The problems of each field of ``model``, the params model or a group,
    inside the groups ``stack``, the params model first. Each field is typed
    as :func:`_type_problems` checks, has no alias, is not excluded from the
    JSON form, and has a default that is a value (see
    :func:`_default_problem`).
    """
    kind = 'a parameter' if len(stack) == 1 else 'a field of a group'
    problems = []
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        if _has_alias(field):
            problems.append(f'{where}: {kind} has no alias')
            continue
        if field.exclude or field.exclude_if is not None:
            problems.append(
                f'{where}: {kind} has no exclude or exclude_if; the record holds '
                'the JSON form, which would leave out the value'
            )
            continue
        metadata = field_types.field_metadata(field)
        if found := _type_problems(field.annotation, metadata, stack):
            problems.extend(f'{where}: {problem}' for problem in found)
        elif field.default_factory_takes_validated_data:
            problems.append(f'{where}: the default_factory takes no other values')
        elif not field.is_required() and (problem := _default_problem(model, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _type_problems(
    annotation: Any, metadata: tuple[Any, ...], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """
    The problems of a field typed ``annotation``, with ``metadata``, inside
    the groups ``stack``, at any depth. The types of a field:

    - a scalar, with constraints;
    - a file field, ``Annotated[Path, File(...)]``;
    - a group, whose fields are typed so in turn, and which does not
      contain itself;
    - a union of groups with a tag;
    - ``Referenced[T]``, ``T`` a group typed as an output is (see
      :func:`_referenced_problems`);
    - a union of members written inline and members ``Referenced[...]``;
    - ``X | None``, and ``list[X]``, with ``X`` not a list and not
      ``X | None``.
    """
    if _marks_optional(annotation, metadata):
        return [_OPTIONAL_MARKED]
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if field_types.is_alias(tp):
        return [_alias_text(tp)]
    if problem := _marker_problem(tp, metadata):
        return [problem]
    if REFERENCED not in metadata and get_origin(tp) is list:
        item, item_none, _ = field_types.unwrapped(get_args(tp)[0])
        if none:
            return ['a list is not typed list[X] | None; a list without elements is []']
        if item_none:
            return [
                'the element of a list is not typed X | None; each element is a value'
            ]
        if get_origin(item) is list:
            return ['a list is not of lists; its element is a group that holds a list']
        return _type_problems(get_args(tp)[0], (), stack)
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    problems = [p for data in referenced for p in _referenced_problems(data)]
    if inline is None:
        return problems
    members = get_args(inline) if get_origin(inline) in (Union, UnionType) else ()
    if (referenced and any(map(field_types.discriminates, metadata))) or any(
        map(field_types.is_referenced, members)
    ):
        return [*problems, _TAGGED_REFERENCE]
    if aliases := [m for m in members if field_types.is_alias(field_types.plain(m))]:
        return [*problems, *map(_alias_text, aliases)]
    if problem := _marker_problem(inline, inline_metadata):
        return [*problems, problem]
    if field_types.file_marker(inline, *inline_metadata) is not None:
        constraints = [m for m in inline_metadata if _is_constraint(m)]
        return [*problems, *map(_constraint_text, constraints)]
    if any(map(field_types.is_file, members)):
        return [*problems, _FILES_UNION]
    try:
        union = field_types.alternatives(inline, *inline_metadata)
    except ValueError as error:
        return [*problems, str(error)]
    if union is not None:
        groups = list(union[1].values())
    elif field_types.is_group(inline):
        groups = [inline]
    elif field_types.is_scalar(inline):
        return problems
    else:
        return [*problems, _not_a_field_type(inline, stack)]
    return [*problems, *(p for g in groups for p in _group_problems(g, stack))]


_TAGGED_REFERENCE = (
    'the discriminator of a union with a member Referenced[...] is on the union '
    "written inline, Annotated[A | B, Field(discriminator='kind')] | "
    'Referenced[C], since a reference to an output has no tag'
)


def _marks_optional(annotation: Any, metadata: tuple[Any, ...]) -> bool:
    """
    Whether a :class:`~ess.spec.parameters.File` marker, in ``metadata`` or
    in an ``Annotated`` of ``annotation``, marks a type ``X | None``.
    """
    tp = annotation
    while True:
        origin, args = get_origin(tp), get_args(tp)
        if origin in (Union, UnionType) and NoneType in args:
            if any(isinstance(m, File) for m in metadata):
                return True
            rest = tuple(a for a in args if a is not NoneType)
            tp = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
        elif origin in (Union, UnionType):
            return any(_marks_optional(arg, metadata) for arg in args)
        elif origin is Annotated:
            metadata = (*metadata, *tp.__metadata__)
            tp = args[0]
        else:
            return False


_OPTIONAL_MARKED = (
    'File(...) marks a Path, not Path | None; a file field that may be None is '
    'typed Annotated[Path, File(...)] | None, as Referenced[X] | None is'
)


def _is_constraint(metadata: Any) -> bool:
    """
    Whether ``metadata`` is a constraint that pydantic applies, such as
    ``MinLen(2)`` or ``Field(gt=2)``, not a validator or a note.
    """
    if isinstance(metadata, FieldInfo):
        return bool(metadata.metadata)
    return isinstance(metadata, BaseMetadata | GroupedMetadata)


def _constraint_text(constraint: Any) -> str:
    return (
        f'a file field takes no constraint, such as {constraint!r}; a check of '
        'its local path is a validator, such as AfterValidator(...), and the '
        'file source checks the file'
    )


_FILES_UNION = (
    'a field takes one kind of file, so a file field is in a union only with '
    'members Referenced[...] and None; the marker of one file field names the '
    'metadata that each file that fits has'
)


def _marker_problem(tp: Any, metadata: tuple[Any, ...]) -> str | None:
    """
    Why the :class:`~ess.spec.parameters.File` markers in ``metadata``, of a
    field of type ``tp``, do not make it a file field, or ``None``: a marker
    marks a ``Path``, once.
    """
    markers = [m for m in metadata if isinstance(m, File)]
    if not markers:
        return None
    if tp is not Path:
        return (
            f'File(...) marks a Path, not {field_types.type_name(tp)}; a file field '
            "is typed Annotated[Path, File(...)], such as Annotated[Path, File("
            "kind='mask')]"
        )
    if len(markers) > 1:
        return f'a file field has one File(...) marker, not {markers}'
    return None


def _not_a_field_type(tp: Any, stack: tuple[type[BaseModel], ...]) -> str:
    """Why ``tp`` is not the type of a field inside the groups ``stack``."""
    name = field_types.type_name(tp)
    if _is_path(tp):
        return _UNMARKED_PATH
    if len(stack) > 1:
        group = stack[-1].__name__
        fix = (
            f'or, if the data of an output fill {group}, type the field that '
            f'holds it Referenced[{group}]'
        )
    else:
        fix = (
            'or, if the data of an output fill it, type it Referenced[G], with G '
            'a group that holds them'
        )
    return (
        f'{name} is not a field type, which is a scalar, a file field, a group, '
        f'a union of groups, Referenced[T], X | None, or list[X]; give the field '
        f'one of these types, {fix}'
    )


def _found(annotation: Any, test: Callable[[Any], bool]) -> list[tuple[str, Any]]:
    """
    Each type that ``annotation`` is made of (see
    :func:`~ess.spec.field_types.reachable`) for which ``test`` holds, with
    where it is: ``''`` in ``annotation`` itself, ``'Class.field: '`` in a
    field of a class in it.
    """
    found = [('', t) for t in field_types.arguments(annotation) if test(t)]
    for cls in dict.fromkeys(field_types.reachable(annotation)):
        found.extend(
            (f'{cls.__name__}.{name}: ', t)
            for name, tp in field_types.fields_of(cls).items()
            for t in field_types.arguments(tp)
            if test(t)
        )
    return found


def _alias_problems(annotation: Any) -> list[str]:
    """
    Refuse each ``NewType`` or type alias that ``annotation`` is made of,
    named by the field of the class that holds it, if any: a class says what
    a value is.
    """
    return [
        f'{where}{_alias_text(alias)}'
        for where, alias in _found(annotation, field_types.is_alias)
    ]


def _alias_text(alias: Any) -> str:
    kind = 'a type alias' if isinstance(alias, TypeAliasType) else 'a NewType'
    return (
        f'{alias.__name__} is {kind}; a spec names a role by a class, such as a '
        'group, and a binding maps it to a key of its own'
    )


def _output_path_problems(annotation: Any) -> list[str]:
    """
    Refuse each ``Path`` that ``annotation``, of an output, is made of, a
    file field among them, named by the field of the class that holds it, if
    any: no output holds one, so that every file comes from the file source,
    which checks it.
    """
    found = _found(annotation, _is_path)
    return [
        f'{where}a Path is in the data of an output; no output holds a file or a '
        'Path, so that every file comes from the file source, which checks it'
        for where in dict.fromkeys(where for where, _ in found)
    ]


_UNMARKED_PATH = (
    'a Path without File(...); a parameter or a field that takes a file is a '
    "file field, such as mask: MaskFile with MaskFile = Annotated[Path, File("
    "kind='mask')], written as file(...)"
)


def _is_path(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, PurePath)


def _has_alias(field: FieldInfo) -> bool:
    return any(
        getattr(field, a) is not None
        for a in ('alias', 'validation_alias', 'serialization_alias')
    )


def _group_problems(
    group: type[BaseModel], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """
    The problems of a group that a request writes, inside the groups
    ``stack``.

    A group is refused if it contains itself, at any depth; if it has a
    serializer or a computed field, which would make its JSON form differ
    from the object of its fields; or for a problem of a field (see
    :func:`_fields_problems`). Field and model validators of any mode are
    allowed: they run whenever the group is validated (see
    :func:`ess.spec.request_types.validated`).
    """
    if group in stack:
        return [
            f'{group.__name__} contains itself; a group holds no field of its own '
            'type, at any depth'
        ]
    problems = []
    if found := _decorators(group, _SERIALIZERS):
        problems.append(f'{group.__name__}: no serializers or computed fields, {found}')
    return [*problems, *_fields_problems(group, (*stack, group))]


def _referenced_problems(data: Any) -> list[str]:
    """
    The problems of ``T`` of ``Referenced[T]``, typed as an output is: a
    group, so that a picker of records lists the outputs of a class that
    names a role, without a type alias or a ``Path``, a file field among
    them, at any depth (see :func:`_output_path_problems`), and without
    ``Referenced[...]`` inside it. The framework does not look inside ``T``
    otherwise.
    """
    name = field_types.type_name(data)
    if not field_types.is_group(group := field_types.plain(data)):
        return [
            f'Referenced[{name}]: {name} is not a group; a reference fills a field '
            'with a group, so that a picker of records lists the outputs of a class '
            'that names a role: mark each group, Referenced[A] | Referenced[B] | None'
        ]
    problems = [
        *_alias_problems(group),
        *_output_path_problems(group),
    ]
    if field_types.holds_ref(group):
        problems.append(
            f'Referenced[{name}]: {name} holds Referenced[...]; a reference is '
            'never inside the data of an output'
        )
    return problems


def _default_problem(model: type[BaseModel], name: str) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value, or
    ``None``: what a request may write, without a reference to an output or
    a file (see :func:`ess.spec.request_types.held`), and the value that
    validating it gives, since pydantic does not validate a default, and a
    binding gets it as it is (see :func:`ess.spec.request_types.validated`). So a
    group's default is an instance, not the object of its fields, and a
    list's default keeps the length constraints of the list. A file field has
    no default but ``None`` or ``[]``: each ``Path`` in the default names a
    file, which the resolver of files refuses.
    """
    default = model.model_fields[name].get_default(call_default_factory=True)
    try:
        held, _ = request_types.held(model, name, default, _no_file, _no_path)
        if refs := [
            (loc, f'{ref} is a reference; a default is a value')
            for loc, ref in iter_refs({name: held})
        ]:
            raise ParamsError(refs)
        made = request_types.validated(model, name, default)
    except ParamsError as error:
        return f'the default is not a value a request may write: {error}'
    if made != default:
        return f'the default {default!r} is not the value validating it gives, {made!r}'
    return None


def _no_file(name: str, marker: File) -> str:
    raise ValueError(_NO_FILE)


def _no_path(ref: FileRef) -> Path:
    raise AssertionError('no file of a default is resolved, so none has a path')


_NO_FILE = 'a default names no file; a file field has no default but None or []'


def _outputs_problems(model: type[BaseModel]) -> list[str]:
    problems = []
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        tp = field.annotation
        if aliases := _alias_problems(tp):
            problems.extend(f'{where}: {problem}' for problem in aliases)
        elif not field_types.is_single(field_types.value_type(field.annotation)):
            problems.append(
                f'{where}: an output is a type, possibly typed X | None, or a '
                'list or a dict of one'
            )
        elif field_types.holds_ref(field.rebuild_annotation()):
            problems.append(
                f'{where}: Referenced[...] marks a field of the params, not an output'
            )
        else:
            problems.extend(
                f'{where}: {problem}' for problem in _output_path_problems(tp)
            )
    return problems


def _tables_problems(spec: WorkflowSpec[Any, Any]) -> list[str]:
    fields = spec.params.model_fields
    problems = [
        f'{name}: a table named twice'
        for name in sorted(set(spec.tables))
        if spec.tables.count(name) > 1
    ]
    for name in dict.fromkeys(spec.tables):
        if (field := fields.get(name)) is None:
            problems.append(f'{name}: not a parameter of {spec.name}')
        elif get_origin(field.annotation) is not list:
            problems.append(f'{name}: a table is a list parameter')
        elif not field.is_required():
            problems.append(f'{name}: a table has no default')
        elif other := [m for m in field.metadata if not isinstance(m, MinLen | MaxLen)]:
            problems.append(
                f'{name}: a table carries only length constraints, not {other}; '
                'an accumulator takes a few rows at a time, so a check of one row '
                'belongs to the element type'
            )
    return problems


def _params_schema(spec: WorkflowSpec[Any, Any]) -> dict[str, Any]:
    """
    The JSON Schema of the params: that of the spec's request type (see
    :func:`ess.spec.request_types.request_model`), which validates a
    request. As the backend does, it refuses a field that a model lacks.
    """
    model = request_types.request_model(spec.params)
    schema = model.model_json_schema()
    for name, prop in schema.get('properties', {}).items():
        # pydantic leaves out the title of a field that is a $ref, if it is
        # the one it would make of the name.
        prop.setdefault('title', model.model_fields[name].title)
    for group in [schema, *schema.get('$defs', {}).values()]:
        if 'properties' in group:
            group['additionalProperties'] = False
    return schema


def _outputs_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The schema of the outputs model, by the type of each output."""
    return {
        'title': model.__name__,
        'type': 'object',
        'properties': {n: _output_property(f) for n, f in model.model_fields.items()},
        'required': [n for n, f in model.model_fields.items() if f.is_required()],
    }


def _output_property(field: FieldInfo) -> dict[str, Any]:
    """The schema of an output, by the type of each value."""
    prop = _output_type_schema(field.annotation)
    if field.title is not None:
        prop['title'] = field.title
    if field.description is not None:
        prop['description'] = field.description
    if not field.is_required():
        prop['default'] = field.get_default(call_default_factory=True)
    for min_len in (m for m in field.metadata if isinstance(m, MinLen)):
        prop['minItems'] = min_len.min_length
    return prop


def _output_type_schema(annotation: Any) -> dict[str, Any]:
    tp, is_optional = field_types.optional(annotation)
    if (item := field_types.element(tp)) is None:
        schema: dict[str, Any] = {'dataType': field_types.type_name(tp)}
    elif get_origin(tp) is list:
        schema = {'type': 'array', 'items': _output_type_schema(item)}
    else:
        schema = {'type': 'object', 'additionalProperties': _output_type_schema(item)}
    return {'anyOf': [schema, {'type': 'null'}]} if is_optional else schema
