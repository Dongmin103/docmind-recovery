from types import SimpleNamespace

import pytest

from api.apps.services import docmind_shared_workspace_service as service


def test_shared_workspace_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DOCMIND_SHARED_WORKSPACE_ENABLED", raising=False)

    assert service.enabled() is False
    with pytest.raises(service.DocmindSharedWorkspaceError, match="DISABLED"):
        service.resolve_user()


def test_shared_workspace_resolves_one_active_configured_user(monkeypatch):
    user = SimpleNamespace(
        id="a" * 32,
        is_active="1",
        access_token="b" * 32,
    )
    monkeypatch.setenv("DOCMIND_SHARED_WORKSPACE_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_SHARED_USER_ID", user.id)
    monkeypatch.setattr(service.UserService, "query", lambda **kwargs: [user])

    assert service.resolve_user() is user


def test_shared_workspace_can_resolve_bootstrapped_user_by_email(monkeypatch):
    user = SimpleNamespace(
        id="a" * 32,
        is_active="1",
        access_token="b" * 32,
    )
    monkeypatch.setenv("DOCMIND_SHARED_WORKSPACE_ENABLED", "1")
    monkeypatch.delenv("DOCMIND_SHARED_USER_ID", raising=False)
    monkeypatch.setenv("DOCMIND_SHARED_USER_EMAIL", "DocMind@Internal.Invalid")
    calls = []
    monkeypatch.setattr(
        service.UserService,
        "query",
        lambda **kwargs: calls.append(kwargs) or [user],
    )

    assert service.resolve_user() is user
    assert calls == [{"email": "docmind@internal.invalid", "status": "1"}]


@pytest.mark.parametrize(
    ("user_id", "users", "code"),
    [
        ("short", [], "DOCMIND_SHARED_USER_INVALID"),
        ("a" * 32, [], "DOCMIND_SHARED_USER_NOT_FOUND"),
        (
            "a" * 32,
            [SimpleNamespace(is_active="0", access_token="b" * 32)],
            "DOCMIND_SHARED_USER_INACTIVE",
        ),
    ],
)
def test_shared_workspace_fails_closed_for_invalid_configuration(
    monkeypatch,
    user_id,
    users,
    code,
):
    monkeypatch.setenv("DOCMIND_SHARED_WORKSPACE_ENABLED", "true")
    monkeypatch.setenv("DOCMIND_SHARED_USER_ID", user_id)
    monkeypatch.setattr(service.UserService, "query", lambda **kwargs: users)

    with pytest.raises(service.DocmindSharedWorkspaceError, match=code):
        service.resolve_user()
