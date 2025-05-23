"""
Flow is the main interface for building and running flows.
"""

from __future__ import annotations

import asyncio
import re
import inspect
import datetime

from typing import Any, Callable, Sequence, TypeVar
from threading import Lock
from enum import Enum
from dataclasses import dataclass

from . import _engine
from . import index
from . import op
from .convert import dump_engine_object
from .typing import encode_enriched_type
from .runtime import execution_context

class _NameBuilder:
    _existing_names: set[str]
    _next_name_index: dict[str, int]

    def __init__(self):
        self._existing_names = set()
        self._next_name_index = {}

    def build_name(self, name: str | None, /, prefix: str) -> str:
        """
        Build a name. If the name is None, generate a name with the given prefix.
        """
        if name is not None:
            self._existing_names.add(name)
            return name

        next_idx = self._next_name_index.get(prefix, 0)
        while True:
            name = f"{prefix}{next_idx}"
            next_idx += 1
            self._next_name_index[prefix] = next_idx
            if name not in self._existing_names:
                self._existing_names.add(name)
                return name


_WORD_BOUNDARY_RE = re.compile('(?<!^)(?=[A-Z])')
def _to_snake_case(name: str) -> str:
    return _WORD_BOUNDARY_RE.sub('_', name).lower()

def _create_data_slice(
        flow_builder_state: _FlowBuilderState,
        creator: Callable[[_engine.DataScopeRef | None, str | None], _engine.DataSlice],
        name: str | None = None) -> DataSlice:
    if name is None:
        return DataSlice(_DataSliceState(
            flow_builder_state,
            lambda target:
                creator(target[0], target[1]) if target is not None else creator(None, None)))
    else:
        return DataSlice(_DataSliceState(flow_builder_state, creator(None, name)))


def _spec_kind(spec: Any) -> str:
    return spec.__class__.__name__

T = TypeVar('T')

class _DataSliceState:
    flow_builder_state: _FlowBuilderState

    _lazy_lock: Lock | None = None  # None means it's not lazy.
    _data_slice: _engine.DataSlice | None = None
    _data_slice_creator: Callable[[tuple[_engine.DataScopeRef, str] | None],
                                  _engine.DataSlice] | None = None

    def __init__(
            self, flow_builder_state: _FlowBuilderState,
            data_slice: _engine.DataSlice | Callable[[tuple[_engine.DataScopeRef, str] | None],
                                                     _engine.DataSlice]):
        self.flow_builder_state = flow_builder_state

        if isinstance(data_slice, _engine.DataSlice):
            self._data_slice = data_slice
        else:
            self._lazy_lock = Lock()
            self._data_slice_creator = data_slice

    @property
    def engine_data_slice(self) -> _engine.DataSlice:
        """
        Get the internal DataSlice.
        """
        if self._lazy_lock is None:
            if self._data_slice is None:
                raise ValueError("Data slice is not initialized")
            return self._data_slice
        else:
            if self._data_slice_creator is None:
                raise ValueError("Data slice creator is not initialized")
            with self._lazy_lock:
                if self._data_slice is None:
                    self._data_slice = self._data_slice_creator(None)
                return self._data_slice

    def attach_to_scope(self, scope: _engine.DataScopeRef, field_name: str) -> None:
        """
        Attach the current data slice (if not yet attached) to the given scope.
        """
        if self._lazy_lock is not None:
            with self._lazy_lock:
                if self._data_slice_creator is None:
                    raise ValueError("Data slice creator is not initialized")
                if self._data_slice is None:
                    self._data_slice = self._data_slice_creator((scope, field_name))
                    return
        # TODO: We'll support this by an identity transformer or "aliasing" in the future.
        raise ValueError("DataSlice is already attached to a field")

class DataSlice:
    """A data slice represents a slice of data in a flow. It's readonly."""

    _state: _DataSliceState

    def __init__(self, state: _DataSliceState):
        self._state = state

    def __str__(self):
        return str(self._state.engine_data_slice)

    def __repr__(self):
        return repr(self._state.engine_data_slice)

    def __getitem__(self, field_name: str) -> DataSlice:
        field_slice = self._state.engine_data_slice.field(field_name)
        if field_slice is None:
            raise KeyError(field_name)
        return DataSlice(_DataSliceState(self._state.flow_builder_state, field_slice))

    def row(self) -> DataScope:
        """
        Return a scope representing each entry of the collection.
        """
        row_scope = self._state.engine_data_slice.collection_entry_scope()
        return DataScope(self._state.flow_builder_state, row_scope)

    def for_each(self, f: Callable[[DataScope], None]) -> None:
        """
        Apply a function to each row of the collection.
        """
        with self.row() as scope:
            f(scope)

    def transform(self, fn_spec: op.FunctionSpec, *args, **kwargs) -> DataSlice:
        """
        Apply a function to the data slice.
        """
        transform_args: list[tuple[Any, str | None]]
        transform_args = [(self._state.engine_data_slice, None)]
        transform_args += [(self._state.flow_builder_state.get_data_slice(v), None) for v in args]
        transform_args += [(self._state.flow_builder_state.get_data_slice(v), k)
                           for (k, v) in kwargs.items()]

        flow_builder_state = self._state.flow_builder_state
        return _create_data_slice(
            flow_builder_state,
            lambda target_scope, name:
                flow_builder_state.engine_flow_builder.transform(
                    _spec_kind(fn_spec),
                    dump_engine_object(fn_spec),
                    transform_args,
                    target_scope,
                    flow_builder_state.field_name_builder.build_name(
                        name, prefix=_to_snake_case(_spec_kind(fn_spec))+'_'),
                ))

    def call(self, func: Callable[[DataSlice], T]) -> T:
        """
        Call a function with the data slice.
        """
        return func(self)

def _data_slice_state(data_slice: DataSlice) -> _DataSliceState:
    return data_slice._state  # pylint: disable=protected-access

class DataScope:
    """
    A data scope in a flow.
    It has multple fields and collectors, and allow users to add new fields and collectors.
    """
    _flow_builder_state: _FlowBuilderState
    _engine_data_scope: _engine.DataScopeRef

    def __init__(self, flow_builder_state: _FlowBuilderState, data_scope: _engine.DataScopeRef):
        self._flow_builder_state = flow_builder_state
        self._engine_data_scope = data_scope

    def __str__(self):
        return str(self._engine_data_scope)

    def __repr__(self):
        return repr(self._engine_data_scope)

    def __getitem__(self, field_name: str) -> DataSlice:
        return DataSlice(_DataSliceState(
            self._flow_builder_state,
            self._flow_builder_state.engine_flow_builder.scope_field(
                self._engine_data_scope, field_name)))

    def __setitem__(self, field_name: str, value: DataSlice):
        value._state.attach_to_scope(self._engine_data_scope, field_name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del self._engine_data_scope

    def add_collector(self, name: str | None = None) -> DataCollector:
        """
        Add a collector to the flow.
        """
        return DataCollector(
            self._flow_builder_state,
            self._engine_data_scope.add_collector(
                self._flow_builder_state.field_name_builder.build_name(name, prefix="_collector_")
            )
        )

class GeneratedField(Enum):
    """
    A generated field is automatically set by the engine.
    """
    UUID = "Uuid"

class DataCollector:
    """A data collector is used to collect data into a collector."""
    _flow_builder_state: _FlowBuilderState
    _engine_data_collector: _engine.DataCollector

    def __init__(self, flow_builder_state: _FlowBuilderState,
                 data_collector: _engine.DataCollector):
        self._flow_builder_state = flow_builder_state
        self._engine_data_collector = data_collector

    def collect(self, **kwargs):
        """
        Collect data into the collector.
        """
        regular_kwargs = []
        auto_uuid_field = None
        for k, v in kwargs.items():
            if isinstance(v, GeneratedField):
                if v == GeneratedField.UUID:
                    if auto_uuid_field is not None:
                        raise ValueError("Only one generated UUID field is allowed")
                    auto_uuid_field = k
                else:
                    raise ValueError(f"Unexpected generated field: {v}")
            else:
                regular_kwargs.append(
                    (k, self._flow_builder_state.get_data_slice(v)))

        self._flow_builder_state.engine_flow_builder.collect(
            self._engine_data_collector, regular_kwargs, auto_uuid_field)

    def export(self, name: str, target_spec: op.StorageSpec, /, *,
              primary_key_fields: Sequence[str],
              vector_indexes: Sequence[index.VectorIndexDef] = (),
              vector_index: Sequence[tuple[str, index.VectorSimilarityMetric]] = (),
              setup_by_user: bool = False):
        """
        Export the collected data to the specified target.

        `vector_index` is for backward compatibility only. Please use `vector_indexes` instead.
        """
        # For backward compatibility only.
        if len(vector_indexes) == 0 and len(vector_index) > 0:
            vector_indexes = [index.VectorIndexDef(field_name=field_name, metric=metric)
                             for field_name, metric in vector_index]

        index_options = index.IndexOptions(
            primary_key_fields=primary_key_fields,
            vector_indexes=vector_indexes,
        )
        self._flow_builder_state.engine_flow_builder.export(
            name, _spec_kind(target_spec), dump_engine_object(target_spec),
            dump_engine_object(index_options), self._engine_data_collector, setup_by_user)

    def declare(self, spec: op.DeclarationSpec):
        """
        Add a declaration to the flow.
        """
        self._flow_builder_state.engine_flow_builder.declare(dump_engine_object(spec))


_flow_name_builder = _NameBuilder()

class _FlowBuilderState:
    """
    A flow builder is used to build a flow.
    """
    engine_flow_builder: _engine.FlowBuilder
    field_name_builder: _NameBuilder

    def __init__(self, /, name: str | None = None):
        flow_name = _flow_name_builder.build_name(name, prefix="_flow_")
        self.engine_flow_builder = _engine.FlowBuilder(flow_name)
        self.field_name_builder = _NameBuilder()

    def get_data_slice(self, v: Any) -> _engine.DataSlice:
        """
        Return a data slice that represents the given value.
        """
        if isinstance(v, DataSlice):
            return v._state.engine_data_slice
        return self.engine_flow_builder.constant(encode_enriched_type(type(v)), v)

@dataclass
class _SourceRefreshOptions:
    """
    Options for refreshing a source.
    """
    refresh_interval: datetime.timedelta | None = None

class FlowBuilder:
    """
    A flow builder is used to build a flow.
    """
    _state: _FlowBuilderState

    def __init__(self, state: _FlowBuilderState):
        self._state = state

    def __str__(self):
        return str(self._state.engine_flow_builder)

    def __repr__(self):
        return repr(self._state.engine_flow_builder)

    def add_source(self, spec: op.SourceSpec, /, *,
            name: str | None = None,
            refresh_interval: datetime.timedelta | None = None,
        ) -> DataSlice:
        """
        Add a source to the flow.
        """
        return _create_data_slice(
            self._state,
            lambda target_scope, name: self._state.engine_flow_builder.add_source(
                _spec_kind(spec),
                dump_engine_object(spec),
                target_scope,
                self._state.field_name_builder.build_name(
                    name, prefix=_to_snake_case(_spec_kind(spec))+'_'),
                dump_engine_object(_SourceRefreshOptions(refresh_interval=refresh_interval)),
            ),
            name
        )

@dataclass
class FlowLiveUpdaterOptions:
    """
    Options for live updating a flow.
    """
    live_mode: bool = True
    print_stats: bool = False

class FlowLiveUpdater:
    """
    A live updater for a flow.
    """
    _engine_live_updater: _engine.FlowLiveUpdater

    def __init__(self, arg: Flow | _engine.FlowLiveUpdater, options: FlowLiveUpdaterOptions | None = None):
        if isinstance(arg, _engine.FlowLiveUpdater):
            self._engine_live_updater = arg
        else:
            self._engine_live_updater = execution_context.run(_engine.FlowLiveUpdater(
                arg.internal_flow(), dump_engine_object(options or FlowLiveUpdaterOptions())))

    @staticmethod
    async def create(fl: Flow, options: FlowLiveUpdaterOptions | None = None) -> FlowLiveUpdater:
        """
        Create a live updater for a flow.
        """
        engine_live_updater = await _engine.FlowLiveUpdater.create(
            await fl.ainternal_flow(),
            dump_engine_object(options or FlowLiveUpdaterOptions()))
        return FlowLiveUpdater(engine_live_updater)

    def __enter__(self) -> FlowLiveUpdater:
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.abort()
        execution_context.run(self.wait())

    async def __aenter__(self) -> FlowLiveUpdater:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.abort()
        await self.wait()

    async def wait(self) -> None:
        """
        Wait for the live updater to finish.
        """
        await self._engine_live_updater.wait()

    def abort(self) -> None:
        """
        Abort the live updater.
        """
        self._engine_live_updater.abort()

    def update_stats(self) -> _engine.IndexUpdateInfo:
        """
        Get the index update info.
        """
        return self._engine_live_updater.index_update_info()


@dataclass
class EvaluateAndDumpOptions:
    """
    Options for evaluating and dumping a flow.
    """
    output_dir: str
    use_cache: bool = True

class Flow:
    """
    A flow describes an indexing pipeline.
    """
    _lazy_engine_flow: Callable[[], _engine.Flow]

    def __init__(self, engine_flow_creator: Callable[[], _engine.Flow]):
        engine_flow = None
        lock = Lock()
        def _lazy_engine_flow() -> _engine.Flow:
            nonlocal engine_flow, lock
            if engine_flow is None:
                with lock:
                    if engine_flow is None:
                        engine_flow = engine_flow_creator()
            return engine_flow
        self._lazy_engine_flow = _lazy_engine_flow

    def __str__(self):
        return str(self._lazy_engine_flow())

    def __repr__(self):
        return repr(self._lazy_engine_flow())

    @property
    def name(self) -> str:
        """
        Get the name of the flow.
        """
        return self._lazy_engine_flow().name()

    async def update(self) -> _engine.IndexUpdateInfo:
        """
        Update the index defined by the flow.
        Once the function returns, the indice is fresh up to the moment when the function is called.
        """
        updater = await FlowLiveUpdater.create(self, FlowLiveUpdaterOptions(live_mode=False))
        await updater.wait()
        return updater.update_stats()

    def evaluate_and_dump(self, options: EvaluateAndDumpOptions):
        """
        Evaluate the flow and dump flow outputs to files.
        """
        return self._lazy_engine_flow().evaluate_and_dump(dump_engine_object(options))

    def internal_flow(self) -> _engine.Flow:
        """
        Get the engine flow.
        """
        return self._lazy_engine_flow()

    async def ainternal_flow(self) -> _engine.Flow:
        """
        Get the engine flow. The async version.
        """
        return await asyncio.to_thread(self.internal_flow)

def _create_lazy_flow(name: str | None, fl_def: Callable[[FlowBuilder, DataScope], None]) -> Flow:
    """
    Create a flow without really building it yet.
    The flow will be built the first time when it's really needed.
    """
    def _create_engine_flow() -> _engine.Flow:
        flow_builder_state = _FlowBuilderState(name=name)
        root_scope = DataScope(
            flow_builder_state, flow_builder_state.engine_flow_builder.root_scope())
        fl_def(FlowBuilder(flow_builder_state), root_scope)
        return flow_builder_state.engine_flow_builder.build_flow(execution_context.event_loop)

    return Flow(_create_engine_flow)


_flows_lock = Lock()
_flows: dict[str, Flow] = {}

def add_flow_def(name: str, fl_def: Callable[[FlowBuilder, DataScope], None]) -> Flow:
    """Add a flow definition to the cocoindex library."""
    with _flows_lock:
        if name in _flows:
            raise KeyError(f"Flow with name {name} already exists")
        fl = _flows[name] = _create_lazy_flow(name, fl_def)
    return fl

def flow_def(name = None) -> Callable[[Callable[[FlowBuilder, DataScope], None]], Flow]:
    """
    A decorator to wrap the flow definition.
    """
    return lambda fl_def: add_flow_def(name or fl_def.__name__, fl_def)

def flow_names() -> list[str]:
    """
    Get the names of all flows.
    """
    with _flows_lock:
        return list(_flows.keys())

def flows() -> list[Flow]:
    """
    Get all flows.
    """
    with _flows_lock:
        return list(_flows.values())

def flow_by_name(name: str) -> Flow:
    """
    Get a flow by name.
    """
    with _flows_lock:
        return _flows[name]

def ensure_all_flows_built() -> None:
    """
    Ensure all flows are built.
    """
    for fl in flows():
        fl.internal_flow()

async def aensure_all_flows_built() -> None:
    """
    Ensure all flows are built.
    """
    for fl in flows():
        await fl.ainternal_flow()

async def update_all_flows(options: FlowLiveUpdaterOptions) -> dict[str, _engine.IndexUpdateInfo]:
    """
    Update all flows.
    """
    await aensure_all_flows_built()
    async def _update_flow(fl: Flow) -> _engine.IndexUpdateInfo:
        updater = await FlowLiveUpdater.create(fl, options)
        await updater.wait()
        return updater.update_stats()
    fls = flows()
    all_stats = await asyncio.gather(*(_update_flow(fl) for fl in fls))
    return {fl.name: stats for fl, stats in zip(fls, all_stats)}

_transient_flow_name_builder = _NameBuilder()
class TransientFlow:
    """
    A transient transformation flow that transforms in-memory data.
    """
    _engine_flow: _engine.TransientFlow

    def __init__(
            self, flow_fn: Callable[..., DataSlice],
            flow_arg_types: Sequence[Any], /, name: str | None = None):

        flow_builder_state = _FlowBuilderState(
            name=_transient_flow_name_builder.build_name(name, prefix="_transient_flow_"))
        sig = inspect.signature(flow_fn)
        if len(sig.parameters) != len(flow_arg_types):
            raise ValueError(
                f"Number of parameters in the flow function ({len(sig.parameters)}) "
                "does not match the number of argument types ({len(flow_arg_types)})")

        kwargs: dict[str, DataSlice] = {}
        for (param_name, param), param_type in zip(sig.parameters.items(), flow_arg_types):
            if param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  inspect.Parameter.KEYWORD_ONLY):
                raise ValueError(f"Parameter {param_name} is not a parameter can be passed by name")
            engine_ds = flow_builder_state.engine_flow_builder.add_direct_input(
                param_name, encode_enriched_type(param_type))
            kwargs[param_name] = DataSlice(_DataSliceState(flow_builder_state, engine_ds))

        output = flow_fn(**kwargs)
        flow_builder_state.engine_flow_builder.set_direct_output(
            _data_slice_state(output).engine_data_slice)
        self._engine_flow = flow_builder_state.engine_flow_builder.build_transient_flow(
            execution_context.event_loop)

    def __str__(self):
        return str(self._engine_flow)

    def __repr__(self):
        return repr(self._engine_flow)

    def internal_flow(self) -> _engine.TransientFlow:
        """
        Get the internal flow.
        """
        return self._engine_flow
