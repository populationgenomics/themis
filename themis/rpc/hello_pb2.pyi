from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class SayHelloRequest(_message.Message):
    __slots__ = ("note",)
    NOTE_FIELD_NUMBER: _ClassVar[int]
    note: str
    def __init__(self, note: _Optional[str] = ...) -> None: ...

class SayHelloResponse(_message.Message):
    __slots__ = ("greeting", "project_id", "analysis_id")
    GREETING_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    greeting: str
    project_id: str
    analysis_id: str
    def __init__(self, greeting: _Optional[str] = ..., project_id: _Optional[str] = ..., analysis_id: _Optional[str] = ...) -> None: ...
