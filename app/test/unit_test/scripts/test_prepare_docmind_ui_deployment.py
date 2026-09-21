from scripts.prepare_docmind_ui_deployment import normalized_mount_source


def test_normalizes_docker_desktop_host_mount_without_changing_other_sources():
    assert normalized_mount_source("bind", "/host_mnt/Users/name/project/logs") == "/Users/name/project/logs"
    assert normalized_mount_source("bind", "/srv/docmind") == "/srv/docmind"
    assert normalized_mount_source("volume", "docker_data") == "docker_data"
