"""Pydantic describes the JSON contract and rejects invalid client input."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, field_validator

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Reference = Annotated[str, Field(min_length=1, max_length=500)]
AliasName = Literal["staging", "production"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelCreate(InputModel):
    team: Slug
    name: Slug
    description: str = Field(default="", max_length=2000)


class ModelInfo(ModelCreate):
    id: int
    created_at: str


class VersionMetadata(InputModel):
    framework: Annotated[str, Field(min_length=1, max_length=80)]
    dataset_ref: Reference
    code_ref: Reference
    environment_ref: Reference
    description: str = Field(default="", max_length=2000)
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=100)
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=30
    )

    @field_validator("parameters")
    @classmethod
    def finite_parameters(cls, value):
        # JSON parsers may accept NaN; persisted metadata must be ordinary JSON.
        json.dumps(value, allow_nan=False)
        return value


class VersionInfo(BaseModel):
    model_id: int
    version: int
    metadata: VersionMetadata
    sha256: str
    artifact_size: int
    created_at: str


class AliasSet(InputModel):
    version: int = Field(gt=0, strict=True)


class AliasInfo(AliasSet):
    model_id: int
    alias: AliasName
    updated_at: str
