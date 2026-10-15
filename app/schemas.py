"""Pydantic models: the contract shapes (docs/contract.md) and the Argon response schemas.

The Argon schemas carry no defaults or numeric bounds: Agent Platform turns them into a
response schema, and out-of-range values are clamped by the pipeline instead of
failing validation. Result-side models added after the first build carry defaults
so the stored sample and older live results still load.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

View = Literal["cam0", "cam1", "cam2"]
Verdict = Literal["automate", "keep_human", "needs_fixture"]
Status = Literal["queued", "running", "done", "failed"]
Stage = Literal["queued", "steps", "verifier", "layout", "robot", "payback", "done"]
Decision = Literal["approved", "flagged"]
ObjectKind = Literal["bin", "fixture", "tool", "part_source", "part_destination", "other"]


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
    # Result side: optional, so results made before the box existed still load.
    box: list[int] = Field(default=[], description="Where in that camera's frame it was seen: "
                           "[ymin, xmin, ymax, xmax] on a 0 to 1000 grid; an empty list if it cannot be boxed.")


class ArgonReason(Reason):
    """Reason as Argon must return it: the box is required, so the response schema asks for it."""
    box: list[int] = Field(description="Where in that camera's frame it was seen, as a box [ymin, xmin, ymax, xmax] "
                           "on a 0 to 1000 grid over the whole frame (y down, x right); an empty list only if "
                           "nothing in the frame can be boxed.")


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
    reasons: list[ArgonReason] = Field(description="Two reasons from what was seen, each citing a camera, a time and a box.")
    occurrences: list[Occurrence] = Field(description="Every occurrence seen, one per cycle.")
    parts: list[str] = Field(description="Names of the parts handled in this step.")
    confidence: float = Field(description="Confidence in this step and its verdict, 0 to 1.")


class TaskGraph(BaseModel):
    cycles: int = Field(description="Number of cycles seen.")
    summary: str = Field(description="One paragraph for a production manager.")
    steps: list[ArgonStep]
    parts: list[Part]


# ------------------------------------------------------ Argon call C (layout)

class LayoutObject(BaseModel):
    name: str = Field(description="Short name, e.g. 'bin A', 'tray', 'screwdriver'.")
    kind: ObjectKind
    x_m: float = Field(description="Centre, metres along the bench front edge from the operator's left-hand corner.")
    y_m: float = Field(description="Centre, metres from the bench front edge away from the operator.")
    length_m: float = Field(description="Size along x, metres.")
    width_m: float = Field(description="Size along y, metres.")
    step_ids: list[str] = Field(description="Ids of the steps that use this object; empty if none.")
    confidence: float = Field(description="Confidence in the position and size, 0 to 1.")


class LayoutStep(BaseModel):
    id: str = Field(description="Step id from the analysis.")
    pick_xy_m: list[float] = Field(description="[x, y] in metres where the part is taken in this step; an empty list if nothing is picked.")
    place_xy_m: list[float] = Field(description="[x, y] in metres where the part is put down in this step; an empty list if nothing is placed.")
    confidence: float = Field(description="Confidence in these positions, 0 to 1.")


class ArgonLayout(BaseModel):
    bench_length_m: float = Field(description="Length of the bench along the operator's front edge, metres.")
    bench_width_m: float = Field(description="Depth of the bench away from the operator, metres.")
    operator_x_m: float = Field(description="Where along the front edge the operator stands, metres from the left corner.")
    objects: list[LayoutObject]
    steps: list[LayoutStep] = Field(description="One entry per step id in the analysis, and only those ids.")
    confidence: float = Field(description="Overall confidence in the scale of this layout, 0 to 1.")
    notes: str = Field(description="One or two sentences on what fixed the scale (a known part, a hand) and what is uncertain.")


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


class PriceBand(BaseModel):
    low: float
    high: float
    currency: str = "USD"
    label: str = "indicative"


class RobotOption(BaseModel):
    """One catalogue arm as judged for this job (the alternatives table)."""
    model: str
    vendor: str = ""
    payload_kg: float
    reach_mm: float
    payload_fits: bool
    reach_fits: Optional[bool] = None  # null when no layout gave a needed reach
    fits: bool
    chosen: bool = False
    price_band: Optional[PriceBand] = None
    note: str = ""
    source: str = ""


class Robot(BaseModel):
    model: str
    payload_kg: float
    reach_mm: float
    needed_payload_kg: float
    fits: bool
    why: str
    # Added with the reach check and the alternatives; all optional so older results still load.
    vendor: str = ""
    gripper: str = ""
    source: str = ""
    needed_reach_mm: Optional[float] = None
    payload_fits: Optional[bool] = None
    reach_fits: Optional[bool] = None
    reach_basis: str = ""
    price_band: Optional[PriceBand] = None
    alternatives: list[RobotOption] = []


class CycleEstimate(BaseModel):
    """A cycle-time estimate from the step durations and a conservative speed factor; no motion planning."""
    human_cycle_s: float
    automated_steps_s: float   # the operator's time on the automate steps today
    robot_cycle_s: float       # automated_steps_s x speed_factor
    operator_cycle_s: float    # the other steps, which the operator keeps
    unassigned_s: float        # cycle time no step covers (walking, waiting)
    parallel_cycle_s: float    # if the robot and the operator work side by side
    speed_factor: float
    method: str


class LayoutAnchor(BaseModel):
    step_id: str
    name: str
    verdict: Verdict
    kind: Literal["pick", "place"]
    x_m: float
    y_m: float


class LayoutObjectOut(BaseModel):
    name: str
    kind: ObjectKind
    x_m: float
    y_m: float
    length_m: float
    width_m: float
    step_ids: list[str] = []
    confidence: float = 0.0


class RobotBase(BaseModel):
    x_m: float
    y_m: float
    rule: str


class Layout(BaseModel):
    source: str
    frame: str
    bench: dict        # {"length_m", "width_m"}
    operator: dict     # {"x_m", "y_m"}
    objects: list[LayoutObjectOut] = []
    anchors: list[LayoutAnchor] = []
    robot_base: Optional[RobotBase] = None
    needed_reach_m: Optional[float] = None
    reach_rule: str = ""
    confidence: float = 0.0
    notes: str = ""


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
    # Added later; optional so the stored sample and older live results still load.
    layout: Optional[Layout] = None
    layout_error: Optional[str] = None  # why the layout pass gave nothing, when it did not
    cycle_estimate: Optional[CycleEstimate] = None


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
