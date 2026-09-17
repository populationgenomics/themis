from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ResolveTokenRequest(_message.Message):
    __slots__ = ("session_token",)
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    session_token: str
    def __init__(self, session_token: _Optional[str] = ...) -> None: ...

class SessionContext(_message.Message):
    __slots__ = ("project_id", "analysis_id")
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    project_id: str
    analysis_id: str
    def __init__(self, project_id: _Optional[str] = ..., analysis_id: _Optional[str] = ...) -> None: ...

class CallerClaim(_message.Message):
    __slots__ = ("calling_as", "session_token")
    CALLING_AS_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    calling_as: _sandbox_options_pb2.CallingAs
    session_token: str
    def __init__(self, calling_as: _Optional[_Union[_sandbox_options_pb2.CallingAs, str]] = ..., session_token: _Optional[str] = ...) -> None: ...
