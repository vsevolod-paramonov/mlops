"""Small SQL layer: each operation owns a connection and a short transaction."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def version_dict(row) -> dict:
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json"))
    return result


class RegistryDB:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            # Roll back any unfinished transaction, including on failed commit.
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())

    def create_model(self, fields: dict) -> dict:
        with self.connection() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO models(team, name, description, created_at) VALUES(?,?,?,?)",
                    (fields["team"], fields["name"], fields["description"], now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("A model with this team and name already exists") from exc
            return dict(conn.execute("SELECT * FROM models WHERE id=?", (cursor.lastrowid,)).fetchone())

    def get_model(self, model_id: int) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
            if row is None:
                raise NotFound("Model not found")
            return dict(row)

    def list_models(self, team, name, limit, offset) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM models
                   WHERE (? IS NULL OR team=?) AND (? IS NULL OR instr(name, ?) > 0)
                   ORDER BY id LIMIT ? OFFSET ?""",
                (team, team, name, name, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def register_version(self, model_id: int, metadata: dict, artifact: dict) -> dict:
        with self.connection() as conn:
            # Serialize number allocation across concurrent requests/processes.
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone() is None:
                raise NotFound("Model not found")
            number = conn.execute(
                "SELECT coalesce(max(version),0)+1 FROM versions WHERE model_id=?", (model_id,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO versions
                   (model_id, version, metadata_json, artifact_key, sha256, artifact_size, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (model_id, number, json.dumps(metadata, allow_nan=False), artifact["artifact_key"],
                 artifact["sha256"], artifact["artifact_size"], now()),
            )
            result = version_dict(conn.execute(
                "SELECT * FROM versions WHERE model_id=? AND version=?", (model_id, number)
            ).fetchone())
            conn.commit()
            return result

    def list_versions(self, model_id: int, limit: int, offset: int) -> list[dict]:
        self.get_model(model_id)
        with self.connection() as conn:
            return [version_dict(row) for row in conn.execute(
                "SELECT * FROM versions WHERE model_id=? ORDER BY version LIMIT ? OFFSET ?",
                (model_id, limit, offset),
            ).fetchall()]

    def get_version(self, model_id: int, version: int) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE model_id=? AND version=?", (model_id, version)
            ).fetchone()
            if row is None:
                raise NotFound("Version not found")
            return version_dict(row)

    def set_alias(self, model_id: int, alias: str, version: int) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM versions WHERE model_id=? AND version=?", (model_id, version)
            ).fetchone() is None:
                raise NotFound("Version not found for this model")
            updated_at = now()
            conn.execute(
                """INSERT INTO aliases(model_id, alias, version, updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(model_id, alias) DO UPDATE SET
                   version=excluded.version, updated_at=excluded.updated_at""",
                (model_id, alias, version, updated_at),
            )
            conn.commit()
            return {"model_id": model_id, "alias": alias, "version": version, "updated_at": updated_at}

    def resolve_alias(self, model_id: int, alias: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT v.* FROM versions v JOIN aliases a
                   ON v.model_id=a.model_id AND v.version=a.version
                   WHERE a.model_id=? AND a.alias=?""", (model_id, alias),
            ).fetchone()
            if row is None:
                raise NotFound("Alias not found")
            return version_dict(row)
