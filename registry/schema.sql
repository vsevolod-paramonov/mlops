-- Metadata belongs in SQLite; model bytes belong in the artifacts directory.
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    team TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (team, name)
);

CREATE TABLE IF NOT EXISTS versions (
    model_id INTEGER NOT NULL REFERENCES models(id),
    version INTEGER NOT NULL CHECK (version > 0),
    metadata_json TEXT NOT NULL,
    artifact_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    artifact_size INTEGER NOT NULL CHECK (artifact_size > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (model_id, version)
);

CREATE TABLE IF NOT EXISTS aliases (
    model_id INTEGER NOT NULL,
    alias TEXT NOT NULL CHECK (alias IN ('staging', 'production')),
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (model_id, alias),
    FOREIGN KEY (model_id, version) REFERENCES versions(model_id, version)
);
