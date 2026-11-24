"""Typed MCP output contracts shared by tool schemas and runtime validation."""
from typing import Literal
from pydantic import BaseModel, Field


class Engine(BaseModel):
    name: str
    version: str


class Limits(BaseModel):
    maxItems: int
    maxUploadBytes: int
    maxPixels: int
    concurrency: int


class Capabilities(BaseModel):
    service: str
    version: str = Field(description='PatchProof service version, matching initialize.serverInfo.version.')
    inputs: list[str]
    limits: Limits
    transport: str
    sdk: str = Field(description='MCP SDK name and installed version; independent of the service version.')
    collapseValidation: bool


class Borders(BaseModel):
    left: int
    top: int
    right: int
    bottom: int


class InputImage(BaseModel):
    width: int
    height: int
    pixelFingerprint: str


class Detection(BaseModel):
    verdict: Literal['PROVEN', 'UNCERTAIN']
    borders: Borders | None = Field(description='Margins relative to the full PNG; null when UNCERTAIN.')
    transparentPadding: Borders = Field(description='Transparent outer margins, not control content padding.')
    score: float | None = Field(description='Detection metric; not a confidence percentage.')
    threshold: float
    reason: str | None
    sizesChecked: list[tuple[int, int]]


class Artifacts(BaseModel):
    borderPreviewId: str | None = None
    stretchPreviewIds: list[str] = Field(default_factory=list)


class ItemError(BaseModel):
    code: str
    message: str


class ItemResult(BaseModel):
    clientItemId: str
    assetId: str | None
    input: InputImage | None = None
    detection: Detection | None = None
    engine: Engine | None = None
    artifacts: Artifacts | None = None
    error: ItemError | None = None


class JobResult(BaseModel):
    jobId: str
    status: Literal['queued','running','completed','completed_with_errors','failed','cancelling','cancelled','interrupted']
    total: int
    completed: int
    results: list[ItemResult]


class JobPage(JobResult):
    offset: int


class PreviewResult(BaseModel):
    artifacts: Artifacts
    downloadPrefix: str
    detection: Detection | None
    error: ItemError | None
