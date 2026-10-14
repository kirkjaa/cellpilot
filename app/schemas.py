"""Pydantic models: the contract shapes (docs/contract.md) and the Argon response schemas.

The Argon schemas carry no defaults or numeric bounds: Vertex turns them into a
response schema, and out-of-range values are clamped by the pipeline instead of
failing validation.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

View = Literal["cam0", "cam1", "cam2"]
Verdict = Literal["automate", "keep_human", "needs_fixture"]
Status = Literal["queued", "running", "done", "failed"]
Stage = Literal["queued", "steps", "verifier", "robot", "payback", "done"]
Decision = Literal["approved", "flagged"]


# ---------------------------------------------------------------- session.json

class Clip(BaseModel):
    view: View
    file: str
    fps: float
    width: int
    height: int
    duration_s: float
    offset_s: float


class Session(BaseModel):
    id: str
    name: str
    kind: str = "sample"
    recorded: str = ""
    synthetic: bool = False
    clips: list[Clip]


# ------------------------------------------------- shared by Argon and results

class Reason(BaseModel):
    text: str = Field(description="What was seen that supports the verdict.")
    view: View = Field(description="The camera where it was seen.")
    t_s: float = Field(description="When it was seen, in cam0 seconds.")


class Occurrence(BaseModel):
    cycle: int = Field(description="Cycle number, counting from 1.")
    start_s: float = Field(description="Start of the step in this cycle, cam0 seconds.")
    end_s: float = Field(description="End of the step in this cycle, cam0 seconds.")


class Part(BaseModel):
    name: str = Field(description="Short name; steps refer to the part by it.")
    mass_g: float = Field(description="Estimated mass in grams.")
    size_mm: list[float] = Field(description="Estimated size in millimetres: [length, width, height].")
    confidence: float = Field(description="Confidence in the estimate, 0 to 1.")


# ------------------------------------------------------- Argon call A (steps)

class ArgonStep(BaseModel):
    id: str = Field(description="s1, s2, ... in the order of one cycle.")
    name: str = Field(description="Short verb phrase, e.g. 'Pick bracket'.")
    verdict: Verdict
    reasons: list[Reason] = Field(description="Two reasons from what was seen, each citing a camera and a time.")
    occurrences: list[Occurrence] = Field(description="Every occurrence seen, one per cycle.")
    parts: list[str] = Field(description="Names of the parts handled in this step.")
    confidence: float = Field(description="Confidence in this step and its verdict, 0 to 1.")


class TaskGraph(BaseModel):
    cycles: int = Field(description="Number of cycles seen.")
    summary: str = Field(description="One paragraph for a production manager.")
    steps: list[ArgonStep]
    parts: list[Part]


# ---------------------------------------------------- Argon call D (verifier)

class Correction(BaseModel):
    cycle: int = Field(description="The cycle of the occurrence being corrected.")
    start_s: float = Field(description="Corrected start, cam0 seconds.")
    end_s: float = Field(description="Corrected end, cam0 seconds.")


class VerifyStep(BaseModel):
    id: str = Field(description="Step id from the analysis being checked.")
    agree: bool = Field(description="The step's boundaries match what is seen in every camera, within one second.")
    timing_delta_s: float = Field(description="Largest difference in seconds between the analysis's boundaries and what is seen.")
    corrected: list[Correction] = Field(description="Corrected start and end for each occurrence off by more than one second; empty otherwise.")
    verdict_agree: bool = Field(description="The verdict matches what is seen.")
    note: str = Field(description="One or two sentences naming any camera where something differs.")


class Verify(BaseModel):
    steps: list[VerifyStep]


# ------------------------------------------------------------- analysis.json

class VerifierCheck(BaseModel):
    agree: bool
    verdict_agree: bool
    timing_delta_s: float
    note: str = ""


class Step(BaseModel):
    id: str
    name: str
    verdict: Verdict
    reasons: list[Reason]
    occurrences: list[Occurrence]
    parts: list[str]
    confidence: float
    mean_duration_s: float
    verifier: Optional[VerifierCheck]  # null when the verifier returned no entry for the step
    preflagged: bool


class Counts(BaseModel):
    automate: int
    keep_human: int
    needs_fixture: int
    total: int


class Robot(BaseModel):
    model: str
    payload_kg: float
    reach_mm: float
    needed_payload_kg: float
    fits: bool
    why: str


class RoiDefaults(BaseModel):
    currency: str = "SGD"
    wage_per_h: float = 14.0
    shifts: int = 2
    hours_per_shift: float = 8
    days_per_year: int = 250
    robot_price: float = 40000
    system_multiple: float = 3.0
    grant_share: float = 0.5
    grant_cap: float = 30000


class Result(BaseModel):
    summary: str
    cycles: int
    cycle_time_s: float
    steps: list[Step]
    parts: list[Part]
    counts: Counts
    automated_share: float
    robot: Robot
    roi_defaults: RoiDefaults


class Usage(BaseModel):
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


class Signoff(BaseModel):
    decision: Decision
    note: str = ""
    engineer: str = ""
    at: str


class SignoffIn(BaseModel):
    decision: Decision
    note: str = Field(default="", max_length=2000)
    engineer: str = Field(default="", max_length=120)


class Analysis(BaseModel):
    id: str
    session_id: str
    status: Status
    stage: Stage
    live: bool
    model: str
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None
    usage: Usage = Usage()
    result: Optional[Result] = None
    signoffs: dict[str, Signoff] = {}
