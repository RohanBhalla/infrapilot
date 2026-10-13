"""Session storage behind a small interface: in memory locally, DynamoDB in AWS (Phase 2)."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Protocol

from infrapilot_assistant.state import DialogState

from .schemas import Message, Session


class SessionStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def append(self, session_id: str, *messages: Message) -> None: ...
    def save_dialog(self, session_id: str, dialog: DialogState) -> None: ...


class InMemorySessionStore:
    """Process-local and lost on restart. Keeps the newest `max_sessions` sessions."""

    def __init__(self, max_sessions: int = 1000):
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions

    def create(self) -> Session:
        s = Session(id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[s.id] = s
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return s

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(session_id)
            return s.model_copy(deep=True) if s else None

    def append(self, session_id: str, *messages: Message) -> None:
        with self._lock:
            self._sessions[session_id].messages.extend(messages)
            self._sessions.move_to_end(session_id)

    def save_dialog(self, session_id: str, dialog: DialogState) -> None:
        with self._lock:
            self._sessions[session_id].dialog = dialog.model_copy(deep=True)


def new_message_id() -> str:
    return uuid.uuid4().hex
