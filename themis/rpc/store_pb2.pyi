from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class PutWorkingDocumentRequest(_message.Message):
    __slots__ = ("markdown",)
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    markdown: str
    def __init__(self, markdown: _Optional[str] = ...) -> None: ...

class PutWorkingDocumentResponse(_message.Message):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: int
    def __init__(self, version: _Optional[int] = ...) -> None: ...

class WorkingDocumentSnapshot(_message.Message):
    __slots__ = ("version", "markdown")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    version: int
    markdown: str
    def __init__(self, version: _Optional[int] = ..., markdown: _Optional[str] = ...) -> None: ...

class WorkspaceChunk(_message.Message):
    __slots__ = ("content",)
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    def __init__(self, content: _Optional[bytes] = ...) -> None: ...

class PutWorkspaceResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
