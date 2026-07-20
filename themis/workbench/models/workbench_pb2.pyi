import datetime

from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Narration(_message.Message):
    __slots__ = ("text",)
    TEXT_FIELD_NUMBER: _ClassVar[int]
    text: str
    def __init__(self, text: _Optional[str] = ...) -> None: ...

class ToolResult(_message.Message):
    __slots__ = ("output", "is_error")
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    output: str
    is_error: bool
    def __init__(self, output: _Optional[str] = ..., is_error: _Optional[bool] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("name", "intent", "command", "result")
    NAME_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    name: str
    intent: str
    command: str
    result: ToolResult
    def __init__(self, name: _Optional[str] = ..., intent: _Optional[str] = ..., command: _Optional[str] = ..., result: _Optional[_Union[ToolResult, _Mapping]] = ...) -> None: ...

class ConversationEvent(_message.Message):
    __slots__ = ("id", "occurred_at", "assistant", "user", "tool")
    ID_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    ASSISTANT_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    id: str
    occurred_at: _timestamp_pb2.Timestamp
    assistant: Narration
    user: Narration
    tool: ToolCall
    def __init__(self, id: _Optional[str] = ..., occurred_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., assistant: _Optional[_Union[Narration, _Mapping]] = ..., user: _Optional[_Union[Narration, _Mapping]] = ..., tool: _Optional[_Union[ToolCall, _Mapping]] = ...) -> None: ...

class Project(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class Analysis(_message.Message):
    __slots__ = ("id", "session_id", "project_id", "prompt", "created_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    project_id: str
    prompt: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., project_id: _Optional[str] = ..., prompt: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CreateAnalysisRequest(_message.Message):
    __slots__ = ("prompt", "project_id")
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    prompt: str
    project_id: str
    def __init__(self, prompt: _Optional[str] = ..., project_id: _Optional[str] = ...) -> None: ...

class CreateAnalysisResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ListAnalysesResponse(_message.Message):
    __slots__ = ("analyses",)
    ANALYSES_FIELD_NUMBER: _ClassVar[int]
    analyses: _containers.RepeatedCompositeFieldContainer[Analysis]
    def __init__(self, analyses: _Optional[_Iterable[_Union[Analysis, _Mapping]]] = ...) -> None: ...

class ListProjectsResponse(_message.Message):
    __slots__ = ("projects",)
    PROJECTS_FIELD_NUMBER: _ClassVar[int]
    projects: _containers.RepeatedCompositeFieldContainer[Project]
    def __init__(self, projects: _Optional[_Iterable[_Union[Project, _Mapping]]] = ...) -> None: ...

class PollResponse(_message.Message):
    __slots__ = ("events", "working_document_version")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    WORKING_DOCUMENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[ConversationEvent]
    working_document_version: int
    def __init__(self, events: _Optional[_Iterable[_Union[ConversationEvent, _Mapping]]] = ..., working_document_version: _Optional[int] = ...) -> None: ...

class WorkingDocument(_message.Message):
    __slots__ = ("version", "markdown")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    version: int
    markdown: str
    def __init__(self, version: _Optional[int] = ..., markdown: _Optional[str] = ...) -> None: ...

class DocumentResponse(_message.Message):
    __slots__ = ("document",)
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    document: WorkingDocument
    def __init__(self, document: _Optional[_Union[WorkingDocument, _Mapping]] = ...) -> None: ...
