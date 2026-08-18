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

class ToolLanguage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TOOL_LANGUAGE_UNSPECIFIED: _ClassVar[ToolLanguage]
    TOOL_LANGUAGE_PYTHON: _ClassVar[ToolLanguage]
    TOOL_LANGUAGE_SHELL: _ClassVar[ToolLanguage]
    TOOL_LANGUAGE_MARKDOWN: _ClassVar[ToolLanguage]
    TOOL_LANGUAGE_JSON: _ClassVar[ToolLanguage]
    TOOL_LANGUAGE_TYPESCRIPT: _ClassVar[ToolLanguage]

class DiffLineKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DIFF_LINE_KIND_UNSPECIFIED: _ClassVar[DiffLineKind]
    DIFF_LINE_KIND_CONTEXT: _ClassVar[DiffLineKind]
    DIFF_LINE_KIND_REMOVED: _ClassVar[DiffLineKind]
    DIFF_LINE_KIND_ADDED: _ClassVar[DiffLineKind]

class SubAgentStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SUB_AGENT_STATUS_UNSPECIFIED: _ClassVar[SubAgentStatus]
    SUB_AGENT_STATUS_RUNNING: _ClassVar[SubAgentStatus]
    SUB_AGENT_STATUS_IDLE: _ClassVar[SubAgentStatus]
    SUB_AGENT_STATUS_DONE: _ClassVar[SubAgentStatus]
TOOL_LANGUAGE_UNSPECIFIED: ToolLanguage
TOOL_LANGUAGE_PYTHON: ToolLanguage
TOOL_LANGUAGE_SHELL: ToolLanguage
TOOL_LANGUAGE_MARKDOWN: ToolLanguage
TOOL_LANGUAGE_JSON: ToolLanguage
TOOL_LANGUAGE_TYPESCRIPT: ToolLanguage
DIFF_LINE_KIND_UNSPECIFIED: DiffLineKind
DIFF_LINE_KIND_CONTEXT: DiffLineKind
DIFF_LINE_KIND_REMOVED: DiffLineKind
DIFF_LINE_KIND_ADDED: DiffLineKind
SUB_AGENT_STATUS_UNSPECIFIED: SubAgentStatus
SUB_AGENT_STATUS_RUNNING: SubAgentStatus
SUB_AGENT_STATUS_IDLE: SubAgentStatus
SUB_AGENT_STATUS_DONE: SubAgentStatus

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

class DiffLine(_message.Message):
    __slots__ = ("kind", "text")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    kind: DiffLineKind
    text: str
    def __init__(self, kind: _Optional[_Union[DiffLineKind, str]] = ..., text: _Optional[str] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("name", "intent", "command", "result", "language", "diff")
    NAME_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    DIFF_FIELD_NUMBER: _ClassVar[int]
    name: str
    intent: str
    command: str
    result: ToolResult
    language: ToolLanguage
    diff: _containers.RepeatedCompositeFieldContainer[DiffLine]
    def __init__(self, name: _Optional[str] = ..., intent: _Optional[str] = ..., command: _Optional[str] = ..., result: _Optional[_Union[ToolResult, _Mapping]] = ..., language: _Optional[_Union[ToolLanguage, str]] = ..., diff: _Optional[_Iterable[_Union[DiffLine, _Mapping]]] = ...) -> None: ...

class SubAgent(_message.Message):
    __slots__ = ("thread_id", "status", "prompt", "summary")
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    thread_id: str
    status: SubAgentStatus
    prompt: str
    summary: str
    def __init__(self, thread_id: _Optional[str] = ..., status: _Optional[_Union[SubAgentStatus, str]] = ..., prompt: _Optional[str] = ..., summary: _Optional[str] = ...) -> None: ...

class ConversationEvent(_message.Message):
    __slots__ = ("id", "occurred_at", "assistant", "user", "tool", "sub_agent")
    ID_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    ASSISTANT_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    SUB_AGENT_FIELD_NUMBER: _ClassVar[int]
    id: str
    occurred_at: _timestamp_pb2.Timestamp
    assistant: Narration
    user: Narration
    tool: ToolCall
    sub_agent: SubAgent
    def __init__(self, id: _Optional[str] = ..., occurred_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., assistant: _Optional[_Union[Narration, _Mapping]] = ..., user: _Optional[_Union[Narration, _Mapping]] = ..., tool: _Optional[_Union[ToolCall, _Mapping]] = ..., sub_agent: _Optional[_Union[SubAgent, _Mapping]] = ...) -> None: ...

class Project(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class VariantClassificationInputs(_message.Message):
    __slots__ = ("transcript", "hgvs_c", "clinical_context")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    HGVS_C_FIELD_NUMBER: _ClassVar[int]
    CLINICAL_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    hgvs_c: str
    clinical_context: str
    def __init__(self, transcript: _Optional[str] = ..., hgvs_c: _Optional[str] = ..., clinical_context: _Optional[str] = ...) -> None: ...

class FreeFormInputs(_message.Message):
    __slots__ = ("prompt",)
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    prompt: str
    def __init__(self, prompt: _Optional[str] = ...) -> None: ...

class AnalysisInputs(_message.Message):
    __slots__ = ("variant_classification", "free_form")
    VARIANT_CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    FREE_FORM_FIELD_NUMBER: _ClassVar[int]
    variant_classification: VariantClassificationInputs
    free_form: FreeFormInputs
    def __init__(self, variant_classification: _Optional[_Union[VariantClassificationInputs, _Mapping]] = ..., free_form: _Optional[_Union[FreeFormInputs, _Mapping]] = ...) -> None: ...

class Analysis(_message.Message):
    __slots__ = ("id", "session_id", "project_id", "created_at", "inputs")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    project_id: str
    created_at: _timestamp_pb2.Timestamp
    inputs: AnalysisInputs
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., project_id: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., inputs: _Optional[_Union[AnalysisInputs, _Mapping]] = ...) -> None: ...

class CreateAnalysisRequest(_message.Message):
    __slots__ = ("project_id", "inputs")
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    project_id: str
    inputs: AnalysisInputs
    def __init__(self, project_id: _Optional[str] = ..., inputs: _Optional[_Union[AnalysisInputs, _Mapping]] = ...) -> None: ...

class CreateAnalysisResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ListAnalysesRequest(_message.Message):
    __slots__ = ("project_id",)
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    project_id: str
    def __init__(self, project_id: _Optional[str] = ...) -> None: ...

class ListAnalysesResponse(_message.Message):
    __slots__ = ("analyses",)
    ANALYSES_FIELD_NUMBER: _ClassVar[int]
    analyses: _containers.RepeatedCompositeFieldContainer[Analysis]
    def __init__(self, analyses: _Optional[_Iterable[_Union[Analysis, _Mapping]]] = ...) -> None: ...

class ListProjectsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListProjectsResponse(_message.Message):
    __slots__ = ("projects",)
    PROJECTS_FIELD_NUMBER: _ClassVar[int]
    projects: _containers.RepeatedCompositeFieldContainer[Project]
    def __init__(self, projects: _Optional[_Iterable[_Union[Project, _Mapping]]] = ...) -> None: ...

class PollRequest(_message.Message):
    __slots__ = ("analysis_id",)
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    def __init__(self, analysis_id: _Optional[str] = ...) -> None: ...

class PollResponse(_message.Message):
    __slots__ = ("events", "working_document_version")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    WORKING_DOCUMENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[ConversationEvent]
    working_document_version: int
    def __init__(self, events: _Optional[_Iterable[_Union[ConversationEvent, _Mapping]]] = ..., working_document_version: _Optional[int] = ...) -> None: ...

class ThreadRequest(_message.Message):
    __slots__ = ("analysis_id", "thread_id")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    thread_id: str
    def __init__(self, analysis_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class ThreadResponse(_message.Message):
    __slots__ = ("events",)
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[ConversationEvent]
    def __init__(self, events: _Optional[_Iterable[_Union[ConversationEvent, _Mapping]]] = ...) -> None: ...

class SteerRequest(_message.Message):
    __slots__ = ("analysis_id", "text")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    text: str
    def __init__(self, analysis_id: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class SteerResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class InterruptRequest(_message.Message):
    __slots__ = ("analysis_id",)
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    def __init__(self, analysis_id: _Optional[str] = ...) -> None: ...

class InterruptResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WorkingDocument(_message.Message):
    __slots__ = ("version", "markdown")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    version: int
    markdown: str
    def __init__(self, version: _Optional[int] = ..., markdown: _Optional[str] = ...) -> None: ...

class DocumentRequest(_message.Message):
    __slots__ = ("analysis_id", "version")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    version: int
    def __init__(self, analysis_id: _Optional[str] = ..., version: _Optional[int] = ...) -> None: ...

class DocumentResponse(_message.Message):
    __slots__ = ("document",)
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    document: WorkingDocument
    def __init__(self, document: _Optional[_Union[WorkingDocument, _Mapping]] = ...) -> None: ...
