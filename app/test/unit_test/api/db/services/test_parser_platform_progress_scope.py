from types import SimpleNamespace

from api.db.services.document_service import _latest_parser_run_tasks


def test_latest_parser_run_tasks_ignores_failed_prior_attempts() -> None:
    tasks = [
        SimpleNamespace(parse_run_id="run-old", create_time=1, progress=-1),
        SimpleNamespace(parse_run_id="run-new", create_time=2, progress=1),
        SimpleNamespace(parse_run_id="run-new", create_time=3, progress=1),
    ]

    scoped = _latest_parser_run_tasks(tasks)

    assert [task.parse_run_id for task in scoped] == ["run-new", "run-new"]
    assert all(task.progress == 1 for task in scoped)


def test_latest_parser_run_tasks_leaves_legacy_tasks_unchanged() -> None:
    tasks = [
        SimpleNamespace(parse_run_id=None, create_time=1, progress=1),
        SimpleNamespace(parse_run_id=None, create_time=2, progress=-1),
    ]

    assert _latest_parser_run_tasks(tasks) is tasks
