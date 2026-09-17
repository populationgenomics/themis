from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class CallingAs(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CALLING_AS_UNSPECIFIED: _ClassVar[CallingAs]
    CALLING_AS_SELF: _ClassVar[CallingAs]
    CALLING_AS_AGENT_SESSION: _ClassVar[CallingAs]
    CALLING_AS_WORKER_SESSION: _ClassVar[CallingAs]

class Caller(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CALLER_UNSPECIFIED: _ClassVar[Caller]
    CALLER_WEB: _ClassVar[Caller]
    CALLER_CLU: _ClassVar[Caller]
    CALLER_AGENT: _ClassVar[Caller]
    CALLER_SANDBOX_WORKER: _ClassVar[Caller]
CALLING_AS_UNSPECIFIED: CallingAs
CALLING_AS_SELF: CallingAs
CALLING_AS_AGENT_SESSION: CallingAs
CALLING_AS_WORKER_SESSION: CallingAs
CALLER_UNSPECIFIED: Caller
CALLER_WEB: Caller
CALLER_CLU: Caller
CALLER_AGENT: Caller
CALLER_SANDBOX_WORKER: Caller
ACCOUNT_ID_FIELD_NUMBER: _ClassVar[int]
account_id: _descriptor.FieldDescriptor
CALLING_AS_FIELD_NUMBER: _ClassVar[int]
calling_as: _descriptor.FieldDescriptor
ADMITS_CALLER_FIELD_NUMBER: _ClassVar[int]
admits_caller: _descriptor.FieldDescriptor
