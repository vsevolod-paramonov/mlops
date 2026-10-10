"""Train two tiny JSON models locally and register them through real HTTP."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def checked(response: httpx.Response) -> dict:
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    name = "linear-demo-" + uuid4().hex[:8]
    output = Path("demo-output") / name
    output.mkdir(parents=True)

    # Known relation y=2*x+1. Train/test split is explicit and reproducible.
    train = [(x, 2 * x + 1) for x in range(5)]
    test = [(x, 2 * x + 1) for x in range(5, 8)]
    dataset = json_bytes({"train": train, "test": test})
    (output / "dataset.json").write_bytes(dataset)
    mean_x = sum(x for x, _ in train) / len(train)
    mean_y = sum(y for _, y in train) / len(train)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in train) / sum((x - mean_x) ** 2 for x, _ in train)
    candidates = [
        {"slope": 0.0, "intercept": mean_y},
        {"slope": slope, "intercept": mean_y - slope * mean_x},
    ]
    project = Path(__file__).resolve().parents[1]
    versions = []
    with httpx.Client(base_url=args.url, timeout=30, trust_env=False) as client:
        checked(client.get("/health"))
        model = checked(client.post("/models", json={
            "team": "mlds_1", "name": name, "description": "Learn the registry with y=2*x+1",
        }))
        model_id = model["id"]
        print(f"1. Создана модель {model_id}: {model['team']}/{name}")
        for index, candidate in enumerate(candidates, start=1):
            artifact = json_bytes(candidate)
            (output / f"model-v{index}.json").write_bytes(artifact)
            mae = sum(abs(candidate["slope"] * x + candidate["intercept"] - y) for x, y in test) / len(test)
            metadata = {
                "framework": f"python-stdlib-{sys.version_info.major}.{sys.version_info.minor}",
                "dataset_ref": "sha256:" + sha256(dataset),
                "code_ref": "sha256:" + sha256(Path(__file__).read_bytes()),
                "environment_ref": "requirements.txt@sha256:" + sha256((project / "requirements.txt").read_bytes()),
                "description": "Mean baseline" if index == 1 else "Least squares linear regression",
                "parameters": {"method": "mean" if index == 1 else "least_squares", "train_rows": len(train)},
                "metrics": {"mae": mae}, "tags": ["synthetic", "tutorial"],
            }
            version = checked(client.post(f"/models/{model_id}/versions",
                data={"metadata": json.dumps(metadata)},
                files={"artifact": (f"model-v{index}.json", artifact, "application/json")},
            ))
            assert version["sha256"] == sha256(artifact)
            versions.append(version)
            print(f"2.{index}. Загружена версия {version['version']}, MAE={mae:.2f}")

        alias_url = f"/models/{model_id}/aliases/production"
        checked(client.put(alias_url, json={"version": versions[0]["version"]}))
        best = min(versions, key=lambda item: item["metadata"]["metrics"]["mae"])
        checked(client.put(alias_url, json={"version": best["version"]}))
        selected = checked(client.get(alias_url))
        print(f"3. Production переключён с v1 на v{selected['version']}")

        # Resolve the alias once, then download the immutable version by number.
        response = client.get(f"/models/{model_id}/versions/{selected['version']}/artifact")
        response.raise_for_status()
        assert sha256(response.content) == selected["sha256"]
        (output / "downloaded.json").write_bytes(response.content)
        loaded = response.json()  # We know this demo artifact is JSON; server does not.
        prediction = loaded["slope"] * 10 + loaded["intercept"]
        assert prediction == 21.0
        print(f"4. Файл скачан, SHA-256 совпал; локальный прогноз для x=10: {prediction}")
        (output / "summary.json").write_bytes(json_bytes({"model": model, "versions": versions, "production": selected}))
    print(f"Готово. Примеры и результат: {output}")


if __name__ == "__main__":
    main()
