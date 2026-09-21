from __future__ import annotations

import os

from api.db.services import UserService
from common.constants import ActiveEnum, StatusEnum

_ENABLED_VALUES = {"1", "true", "yes", "on"}


class DocmindSharedWorkspaceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def enabled() -> bool:
    return os.environ.get("DOCMIND_SHARED_WORKSPACE_ENABLED", "").strip().lower() in _ENABLED_VALUES


def resolve_user():
    if not enabled():
        raise DocmindSharedWorkspaceError("DOCMIND_SHARED_WORKSPACE_DISABLED")

    user_id = os.environ.get("DOCMIND_SHARED_USER_ID", "").strip()
    if user_id:
        if len(user_id) != 32:
            raise DocmindSharedWorkspaceError("DOCMIND_SHARED_USER_INVALID")
        users = UserService.query(id=user_id, status=StatusEnum.VALID.value)
    else:
        email = os.environ.get("DOCMIND_SHARED_USER_EMAIL", "").strip().lower()
        if not email:
            raise DocmindSharedWorkspaceError("DOCMIND_SHARED_USER_INVALID")
        users = UserService.query(email=email, status=StatusEnum.VALID.value)
    if len(users) != 1:
        raise DocmindSharedWorkspaceError("DOCMIND_SHARED_USER_NOT_FOUND")

    user = users[0]
    access_token = str(user.access_token or "").strip()
    if user.is_active != ActiveEnum.ACTIVE.value or len(access_token) < 32 or access_token.startswith("INVALID_"):
        raise DocmindSharedWorkspaceError("DOCMIND_SHARED_USER_INACTIVE")
    return user
