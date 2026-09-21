from types import SimpleNamespace

from api.db import init_data
from api.db.joint_services import user_account_service


def test_shared_user_bootstrap_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DOCMIND_SHARED_WORKSPACE_ENABLED", raising=False)
    monkeypatch.setattr(
        init_data.UserService,
        "query",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected query")),
    )

    assert init_data.init_docmind_shared_user() is None


def test_shared_user_bootstrap_reuses_existing_identity(monkeypatch):
    user = SimpleNamespace(id="a" * 32, access_token="b" * 32)
    monkeypatch.setenv("DOCMIND_SHARED_WORKSPACE_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_SHARED_USER_EMAIL", "DOCMIND@INTERNAL.INVALID")
    monkeypatch.setattr(init_data.UserService, "query", lambda **_kwargs: [user])
    monkeypatch.setattr(
        init_data.UserService,
        "update_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected update")),
    )

    assert init_data.init_docmind_shared_user() == user.id


def test_shared_user_bootstrap_creates_one_internal_identity(monkeypatch):
    monkeypatch.setenv("DOCMIND_SHARED_WORKSPACE_ENABLED", "true")
    monkeypatch.setenv("DOCMIND_SHARED_USER_EMAIL", "docmind@internal.invalid")
    monkeypatch.setattr(init_data.UserService, "query", lambda **_kwargs: [])
    captured = []
    monkeypatch.setattr(
        user_account_service,
        "create_new_user",
        lambda payload: captured.append(payload)
        or {"success": True, "user_info": {**payload, "id": "c" * 32}},
    )

    assert init_data.init_docmind_shared_user() == "c" * 32
    assert len(captured) == 1
    assert captured[0]["email"] == "docmind@internal.invalid"
    assert captured[0]["login_channel"] == "internal-shared-workspace"
    assert len(captured[0]["password"]) >= 48
