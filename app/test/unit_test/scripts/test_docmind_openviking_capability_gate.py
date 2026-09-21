from scripts import docmind_openviking_capability_gate as gate


class FakeClient:
    def __init__(self):
        self.sources = {}
        self.calls = []
        self.mutate_after_restart = False
        self.restarted = False

    def running_tasks(self):
        return []

    def add_manifest(self, root_uri, content):
        self.calls.extend(["/api/v1/resources/temp_upload", "/api/v1/resources"])
        self.sources[root_uri] = content.decode()

    def tree(self, root_uri):
        self.calls.append("/api/v1/fs/tree")
        if root_uri not in self.sources:
            return []
        return [
            {"uri": root_uri + suffix}
            for suffix in sorted(gate.EXPECTED_ENTRY_SUFFIXES)
        ]

    def content(self, endpoint, uri):
        self.calls.append(endpoint)
        root_uri = next(root for root in self.sources if uri.startswith(root))
        if endpoint.endswith("read"):
            return self.sources[root_uri]
        suffix = "abstract" if endpoint.endswith("abstract") else "overview"
        changed = "-changed" if self.restarted and self.mutate_after_restart else ""
        return f"{root_uri}:{suffix}{changed}"

    def no_watch(self, target_uri):
        self.calls.append("/api/v1/watches")
        return True

    def get(self, path, **query):
        self.calls.append(path)
        if path == "/api/v1/system/status":
            return {"initialized": True}
        raise AssertionError(path)


def test_capability_gate_proves_restart_and_old_root_persistence():
    client = FakeClient()
    runtime = {"container_id": "container-1", "image_id": "image-1"}

    def restart():
        client.restarted = True

    result = gate.run_gate(
        client,
        restart=restart,
        docker_identity=lambda: dict(runtime),
        nonce="test",
    )

    assert result["status"] == "PASS"
    assert result["first_before_restart"] == result["first_after_restart"]
    assert result["older_root_after_newer_root_sha256"] == result["first_before_restart"]["identity_sha256"]
    assert result["older_root_after_simulated_pointer_sha256"] == result["first_before_restart"]["identity_sha256"]
    assert result["missing_root_detected"] is True
    assert result["active_catalog_or_serving_root_changed"] is False


def test_capability_gate_fails_when_sidecars_change_across_restart():
    client = FakeClient()
    client.mutate_after_restart = True

    def restart():
        client.restarted = True

    try:
        gate.run_gate(
            client,
            restart=restart,
            docker_identity=lambda: {
                "container_id": "container-1",
                "image_id": "image-1",
            },
            nonce="drift",
        )
    except gate.CapabilityError as error:
        assert "changed across restart" in str(error)
    else:
        raise AssertionError("restart drift must fail the capability gate")


def test_root_identity_rejects_extra_layout_entry():
    client = FakeClient()
    root = "viking://resources/probe/"
    source = b"probe"
    client.sources[root] = source.decode()
    original_tree = client.tree

    def tree_with_extra(root_uri):
        return original_tree(root_uri) + [{"uri": root_uri + "unexpected"}]

    client.tree = tree_with_extra
    try:
        gate.root_identity(client, root, gate._sha_bytes(source))
    except gate.CapabilityError as error:
        assert "nine-entry" in str(error)
    else:
        raise AssertionError("unexpected root layout must fail")


def test_wait_root_identity_allows_bounded_root_summary_finalization():
    client = FakeClient()
    root = "viking://resources/probe/"
    source = b"probe"
    client.sources[root] = source.decode()
    calls = 0
    original_tree = client.tree

    def delayed_tree(root_uri):
        nonlocal calls
        calls += 1
        rows = original_tree(root_uri)
        if calls == 1:
            return [
                row
                for row in rows
                if row["uri"][len(root_uri) :]
                not in {".abstract.md", ".overview.md"}
            ]
        return rows

    client.tree = delayed_tree
    identity = gate.wait_root_identity(
        client,
        root,
        gate._sha_bytes(source),
        timeout_seconds=5,
        sleep=lambda _seconds: None,
    )

    assert calls == 2
    assert identity["entry_count"] == 9
