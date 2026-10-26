import datetime

from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class JobEvent(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_EVENT_UNSPECIFIED: _ClassVar[JobEvent]
    JOB_EVENT_BEGUN: _ClassVar[JobEvent]
    JOB_EVENT_SETTLED: _ClassVar[JobEvent]
    JOB_EVENT_REFUSED: _ClassVar[JobEvent]
    JOB_EVENT_FAILED: _ClassVar[JobEvent]

class SettledOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SETTLED_OUTCOME_UNSPECIFIED: _ClassVar[SettledOutcome]
    SETTLED_OUTCOME_NOTHING: _ClassVar[SettledOutcome]
    SETTLED_OUTCOME_PERMANENT_FAILURE: _ClassVar[SettledOutcome]
JOB_EVENT_UNSPECIFIED: JobEvent
JOB_EVENT_BEGUN: JobEvent
JOB_EVENT_SETTLED: JobEvent
JOB_EVENT_REFUSED: JobEvent
JOB_EVENT_FAILED: JobEvent
SETTLED_OUTCOME_UNSPECIFIED: SettledOutcome
SETTLED_OUTCOME_NOTHING: SettledOutcome
SETTLED_OUTCOME_PERMANENT_FAILURE: SettledOutcome

class WorkRequest(_message.Message):
    __slots__ = ("doc_id", "targets", "run_id")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    targets: _containers.RepeatedScalarFieldContainer[str]
    run_id: str
    def __init__(self, doc_id: _Optional[str] = ..., targets: _Optional[_Iterable[str]] = ..., run_id: _Optional[str] = ...) -> None: ...

class JobLogEntry(_message.Message):
    __slots__ = ("at", "attempt", "queue", "task", "retry_count", "step", "inputs", "event", "outcome", "model_answered", "detail")
    AT_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    QUEUE_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    RETRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    STEP_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    MODEL_ANSWERED_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    at: _timestamp_pb2.Timestamp
    attempt: str
    queue: str
    task: str
    retry_count: int
    step: str
    inputs: _containers.RepeatedScalarFieldContainer[str]
    event: JobEvent
    outcome: SettledOutcome
    model_answered: bool
    detail: str
    def __init__(self, at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., attempt: _Optional[str] = ..., queue: _Optional[str] = ..., task: _Optional[str] = ..., retry_count: _Optional[int] = ..., step: _Optional[str] = ..., inputs: _Optional[_Iterable[str]] = ..., event: _Optional[_Union[JobEvent, str]] = ..., outcome: _Optional[_Union[SettledOutcome, str]] = ..., model_answered: _Optional[bool] = ..., detail: _Optional[str] = ...) -> None: ...

class JobLog(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[JobLogEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[JobLogEntry, _Mapping]]] = ...) -> None: ...
