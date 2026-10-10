"""Integration tests: HTTP requests use a real, isolated SQLite DB and file store.

No trained model or external service is needed. Bytes stand in for an opaque
model archive: the registry must preserve them without loading or executing it.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from registry.app import create_app


def metadata(**overrides):
    """Small but reproducible description of a training run."""
    result = {
        "framework": "scikit-learn==1.5.2",
        "dataset_ref": "datasets/churn.csv@sha256:demo",
        "code_ref": "git:0123456789abcdef",
        "environment_ref": "requirements.txt@sha256:demo",
        "metrics": {"accuracy": 0.91},
        "parameters": {"seed": 42, "layers": [16, 8]},
        "tags": ["baseline"],
        "description": "First training run",
    }
    result.update(overrides)
    return result


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as test_client:
        yield test_client


def create_model(client, team="mlds_1", name="churn", **extra):
    response = client.post("/models", json={"team": team, "name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, model_id, payload=b"model bytes", details=None, filename="model.bin"):
    return client.post(
        f"/models/{model_id}/versions",
        data={"metadata": json.dumps(metadata() if details is None else details)},
        files={"artifact": (filename, payload, "application/octet-stream")},
    )


def stored_files(data_dir: Path):
    """Include temporary files so failed uploads cannot hide leftover data."""
    return [path for path in (data_dir / "artifacts").rglob("*") if path.is_file()]


def test_register_download_round_trip(client):
    model = create_model(client, description="Predict customer churn")
    assert model["description"] == "Predict customer churn"
    assert model["created_at"]
    assert client.get(f"/models/{model['id']}").json() == model
    assert client.get("/health").json() == {"status": "ok"}

    payload = b"\x00opaque model archive\xff\x01"
    response = upload(client, model["id"], payload)
    assert response.status_code == 201, response.text
    version = response.json()
    assert version["model_id"] == model["id"]
    assert version["version"] == 1
    assert version["created_at"]
    assert version["metadata"] == metadata()
    assert version["artifact_size"] == len(payload)
    assert version["sha256"] == hashlib.sha256(payload).hexdigest()

    version_url = f"/models/{model['id']}/versions/1"
    assert client.get(version_url).json() == version
    assert client.get(f"/models/{model['id']}/versions").json() == [version]
    download = client.get(f"{version_url}/artifact")
    assert download.status_code == 200
    assert download.content == payload
    assert download.headers["x-artifact-sha256"] == version["sha256"]


def test_model_identity_is_team_and_name_and_search_filters(client):
    first = create_model(client, team="mlds_1", name="churn")
    create_model(client, team="mlds_1", name="ranking")
    second_team = create_model(client, team="mlds_3", name="churn")
    duplicate = client.post("/models", json={"team": "mlds_1", "name": "churn"})
    assert duplicate.status_code == 409
    assert first["description"] == ""
    assert first["id"] != second_team["id"]

    filtered = client.get("/models", params={"team": "mlds_1", "name": "hur"})
    assert filtered.status_code == 200
    assert filtered.json() == [first]
    assert len(client.get("/models", params={"name": "churn"}).json()) == 2
    assert client.get("/models", params={"team": "missing"}).json() == []
    page_1 = client.get("/models", params={"limit": 1, "offset": 0}).json()
    page_2 = client.get("/models", params={"limit": 1, "offset": 1}).json()
    assert len(page_1) == len(page_2) == 1
    assert page_1[0]["id"] != page_2[0]["id"]


@pytest.mark.parametrize("field,value", [("team", "../outside"), ("name", "A Model"), ("name", "")])
def test_invalid_model_names_are_rejected(client, field, value):
    body = {"team": "mlds_1", "name": "churn", field: value}
    assert client.post("/models", json=body).status_code == 422
    assert client.get("/models").json() == []


def test_versions_are_immutable_and_upload_never_overwrites(client):
    model_id = create_model(client)["id"]
    original = upload(client, model_id, b"original").json()
    newer = upload(client, model_id, b"new training run", metadata(description="Run 2")).json()
    assert (original["version"], newer["version"]) == (1, 2)
    assert original["sha256"] != newer["sha256"]

    url = f"/models/{model_id}/versions/1"
    assert client.put(url, json={"metadata": metadata(description="overwrite")}).status_code == 405
    assert client.get(url).json() == original
    assert client.get(f"{url}/artifact").content == b"original"
    assert client.get(f"/models/{model_id}/versions/2/artifact").content == b"new training run"


def test_alias_can_promote_and_roll_back_without_mutating_versions(client):
    model_id = create_model(client)["id"]
    first = upload(client, model_id, b"first").json()
    second = upload(client, model_id, b"second").json()
    alias_url = f"/models/{model_id}/aliases/production"
    assert client.get(alias_url).status_code == 404

    for target in (first, second, first):
        response = client.put(alias_url, json={"version": target["version"]})
        assert response.status_code == 200, response.text
        assert response.json()["version"] == target["version"]
        assert response.json()["alias"] == "production"
        assert response.json()["model_id"] == model_id
        assert response.json()["updated_at"]
        assert client.get(alias_url).json() == target
    assert client.get(f"/models/{model_id}/versions").json() == [first, second]


def test_alias_cannot_reference_another_models_version(client):
    first_id = create_model(client, name="first")["id"]
    second_id = create_model(client, name="second")["id"]
    first_version = upload(client, first_id).json()
    upload(client, second_id)
    upload(client, second_id)
    alias_url = f"/models/{first_id}/aliases/staging"
    assert client.put(alias_url, json={"version": 1}).status_code == 200
    assert client.put(alias_url, json={"version": 2}).status_code == 404
    assert client.get(alias_url).json() == first_version
    assert client.put(f"/models/{first_id}/aliases/arbitrary", json={"version": 1}).status_code == 422


@pytest.mark.parametrize(
    "raw_metadata",
    [
        "not JSON",
        "[]",
        json.dumps({key: value for key, value in metadata().items() if key != "framework"}),
        json.dumps(metadata(dataset_ref="")),
        json.dumps(metadata(metrics={"accuracy": "excellent"})),
        json.dumps(metadata(metrics={"accuracy": float("nan")})),
        json.dumps(metadata(metrics={"accuracy": float("inf")})),
        json.dumps(metadata(parameters={"optimizer": {"learning_rate": float("nan")}})),
    ],
)
def test_invalid_metadata_has_no_side_effects(client, tmp_path, raw_metadata):
    model_id = create_model(client)["id"]
    response = client.post(
        f"/models/{model_id}/versions",
        data={"metadata": raw_metadata},
        files={"artifact": ("model.bin", b"invalid request", "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/models/{model_id}/versions").json() == []
    assert stored_files(tmp_path) == []
    # A rejected attempt must not consume the first version number.
    accepted = upload(client, model_id)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["version"] == 1


@pytest.mark.parametrize("payload,status", [(b"", 400), (b"123456789", 413)])
def test_invalid_artifact_size_leaves_no_orphan_file_or_version(tmp_path, payload, status):
    with TestClient(create_app(data_dir=tmp_path, max_artifact_bytes=8)) as client:
        model_id = create_model(client)["id"]
        response = upload(client, model_id, payload)
        assert response.status_code == status, response.text
        assert client.get(f"/models/{model_id}/versions").json() == []
        assert stored_files(tmp_path) == []
        accepted = upload(client, model_id, b"12345678")
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["version"] == 1


def test_missing_resources_return_404_and_upload_does_not_leave_files(client, tmp_path):
    missing = 999999
    assert client.get(f"/models/{missing}/versions").status_code == 404
    assert client.get(f"/models/{missing}/versions/1").status_code == 404
    assert client.get(f"/models/{missing}/versions/1/artifact").status_code == 404
    assert upload(client, missing).status_code == 404
    assert client.put(f"/models/{missing}/aliases/production", json={"version": 1}).status_code == 404
    assert stored_files(tmp_path) == []
    model_id = create_model(client)["id"]
    assert client.get(f"/models/{model_id}/versions/1").status_code == 404
    assert client.get(f"/models/{model_id}/versions/1/artifact").status_code == 404


def test_client_filename_cannot_escape_artifact_store(tmp_path):
    data_dir = tmp_path / "registry-data"
    with TestClient(create_app(data_dir=data_dir)) as client:
        model_id = create_model(client)["id"]
        response = upload(client, model_id, b"safe bytes", filename="../../escaped-model.pkl")
        assert response.status_code == 201, response.text
        assert client.get(f"/models/{model_id}/versions/1/artifact").content == b"safe bytes"
    blobs = stored_files(data_dir)
    assert len(blobs) == 1
    assert blobs[0].suffix == ".blob"
    assert blobs[0].resolve().parent == (data_dir / "artifacts").resolve()
    assert not list(tmp_path.rglob("escaped-model.pkl"))


def test_concurrent_uploads_get_distinct_sequential_versions(client):
    model_id = create_model(client)["id"]

    def register(index):
        payload = f"model trained in run {index}".encode()
        response = upload(client, model_id, payload)
        assert response.status_code == 201, response.text
        return response.json(), payload

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(register, range(10)))
    assert sorted(version["version"] for version, _ in results) == list(range(1, 11))
    assert len(client.get(f"/models/{model_id}/versions").json()) == 10
    for version, expected_bytes in results:
        url = f"/models/{model_id}/versions/{version['version']}/artifact"
        assert client.get(url).content == expected_bytes


def test_registry_survives_application_restart(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        model = create_model(client)
        version = upload(client, model["id"], b"persistent model").json()
        alias_url = f"/models/{model['id']}/aliases/production"
        assert client.put(alias_url, json={"version": 1}).status_code == 200
    assert (tmp_path / "registry.sqlite3").is_file()

    with TestClient(create_app(data_dir=tmp_path)) as restarted:
        assert restarted.get("/models").json() == [model]
        assert restarted.get(alias_url).json() == version
        assert restarted.get(f"/models/{model['id']}/versions/1/artifact").content == b"persistent model"
        next_version = upload(restarted, model["id"], b"second run")
        assert next_version.status_code == 201, next_version.text
        assert next_version.json()["version"] == 2


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_missing_or_corrupt_artifact_is_reported_instead_of_served(client, tmp_path, damage):
    model_id = create_model(client)["id"]
    version = upload(client, model_id, b"original").json()
    artifact_path, = stored_files(tmp_path)
    if damage == "delete":
        artifact_path.unlink()
    else:
        # Same length: a file-size-only integrity check would miss this damage.
        artifact_path.write_bytes(b"modified")
    url = f"/models/{model_id}/versions/1"
    assert client.get(f"{url}/artifact").status_code == 409
    assert client.get(url).json() == version


def test_failed_commit_rolls_back_insert_and_removes_only_new_artifact(client, tmp_path, monkeypatch):
    model_id = create_model(client)["id"]
    original = upload(client, model_id, b"already registered").json()
    original_files = stored_files(tmp_path)
    real_connection = client.app.state.db.connection
    failed_commits = []

    class FailAtCommit:
        """Forward real SQL operations, but fail before persisting the transaction."""

        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            assert self.connection.in_transaction
            rows = self.connection.execute(
                "SELECT version FROM versions WHERE model_id=? ORDER BY version", (model_id,)
            ).fetchall()
            # Prove failure occurs AFTER INSERT: the uncommitted v2 is visible here.
            assert [row["version"] for row in rows] == [1, 2]
            assert len(stored_files(tmp_path)) == 2
            failed_commits.append(True)
            raise sqlite3.OperationalError("simulated commit failure")

    @contextmanager
    def connection_with_failed_commit():
        # Keep RegistryDB's real context manager, including rollback and close.
        with real_connection() as connection:
            yield FailAtCommit(connection)

    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.db, "connection", connection_with_failed_commit)
        failed = upload(client, model_id, b"must be rolled back")
    assert failed.status_code == 503, failed.text
    assert failed_commits == [True]
    assert stored_files(tmp_path) == original_files
    assert client.get(f"/models/{model_id}/versions").json() == [original]
    assert client.get(f"/models/{model_id}/versions/2").status_code == 404
    assert client.get(f"/models/{model_id}/versions/1/artifact").content == b"already registered"
    retry = upload(client, model_id, b"retry after recovery")
    assert retry.status_code == 201, retry.text
    assert retry.json()["version"] == 2


def test_version_pagination_is_ordered_and_has_no_overlap(client):
    model_id = create_model(client)["id"]
    versions = []
    for index in range(4):
        response = upload(client, model_id, f"run {index}".encode())
        assert response.status_code == 201, response.text
        versions.append(response.json())
    url = f"/models/{model_id}/versions"
    assert client.get(url, params={"limit": 2, "offset": 0}).json() == versions[:2]
    assert client.get(url, params={"limit": 2, "offset": 2}).json() == versions[2:]
    assert client.get(url, params={"limit": 2, "offset": 4}).json() == []
    assert client.get(url, params={"limit": 0}).status_code == 422
