"""Session storage behind a small interface: in memory locally, DynamoDB in AWS (dynamo.py).

Sessions belong to a user, and a user can only reach their own sessions."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Protocol

from infrapilot_assistant.state import DialogState

from .schemas import Message, Session


class SessionStore(Protocol):
    def create(self, user: str) -> Session: ...
    def get(self, user: str, session_id: str) -> Session | None: ...
    def append(self, user: str, session_id: str, *messages: Message) -> None: ...
    def save_dialog(self, user: str, session_id: str, dialog: DialogState) -> None: ...


class InMemorySessionStore:
    """Process-local and lost on restart. Keeps the newest `max_sessions` sessions."""

    def __init__(self, max_sessions: int = 1000):
        self._sessions: OrderedDict[tuple[str, str], Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions

    def create(self, user: str) -> Session:
        s = Session(id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[(user, s.id)] = s
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return s

    def get(self, user: str, session_id: str) -> Session | None:
        with self._lock:
            s = self._sessions.get((user, session_id))
            return s.model_copy(deep=True) if s else None

    def append(self, user: str, session_id: str, *messages: Message) -> None:
        with self._lock:
            self._sessions[(user, session_id)].messages.extend(messages)
            self._sessions.move_to_end((user, session_id))

    def save_dialog(self, user: str, session_id: str, dialog: DialogState) -> None:
        with self._lock:
            self._sessions[(user, session_id)].dialog = dialog.model_copy(deep=True)


def new_message_id() -> str:
    return uuid.uuid4().hex
