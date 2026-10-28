from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Role(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROLE_UNSPECIFIED: _ClassVar[Role]
    ROLE_MANAGER: _ClassVar[Role]
    ROLE_CURATOR: _ClassVar[Role]
ROLE_UNSPECIFIED: Role
ROLE_MANAGER: Role
ROLE_CURATOR: Role

class ResolveTokenRequest(_message.Message):
    __slots__ = ("session_token",)
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    session_token: str
    def __init__(self, session_token: _Optional[str] = ...) -> None: ...

class ProjectGrant(_message.Message):
    __slots__ = ("id", "roles")
    ID_FIELD_NUMBER: _ClassVar[int]
    ROLES_FIELD_NUMBER: _ClassVar[int]
    id: str
    roles: _containers.RepeatedScalarFieldContainer[Role]
    def __init__(self, id: _Optional[str] = ..., roles: _Optional[_Iterable[_Union[Role, str]]] = ...) -> None: ...

class SessionContext(_message.Message):
    __slots__ = ("analysis_id", "project")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    project: ProjectGrant
    def __init__(self, analysis_id: _Optional[str] = ..., project: _Optional[_Union[ProjectGrant, _Mapping]] = ...) -> None: ...

class GetAccessRequest(_message.Message):
    __slots__ = ("email",)
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    email: str
    def __init__(self, email: _Optional[str] = ...) -> None: ...

class GetAccessResponse(_message.Message):
    __slots__ = ("projects",)
    PROJECTS_FIELD_NUMBER: _ClassVar[int]
    projects: _containers.RepeatedCompositeFieldContainer[ProjectGrant]
    def __init__(self, projects: _Optional[_Iterable[_Union[ProjectGrant, _Mapping]]] = ...) -> None: ...

class CallerClaim(_message.Message):
    __slots__ = ("calling_as", "session_token")
    CALLING_AS_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    calling_as: _sandbox_options_pb2.CallingAs
    session_token: str
    def __init__(self, calling_as: _Optional[_Union[_sandbox_options_pb2.CallingAs, str]] = ..., session_token: _Optional[str] = ...) -> None: ...
