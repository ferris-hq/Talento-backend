"""Pose metrics, quality gate and scoring on synthetic skeletons (no model needed)."""

import math

import numpy as np
import pytest

from worker.pose import metrics, quality, scoring
from worker.pose.landmarker import PoseSeries

# Standing figure in normalised image space (y grows downwards), 33 BlazePose points.
BASE = {
    0: (0.50, 0.20),
    11: (0.45, 0.30), 12: (0.55, 0.30),  # shoulders
    13: (0.42, 0.40), 14: (0.58, 0.40),  # elbows
    15: (0.41, 0.50), 16: (0.59, 0.50),  # wrists
    23: (0.47, 0.50), 24: (0.53, 0.50),  # hips
    25: (0.47, 0.65), 26: (0.53, 0.65),  # knees
    27: (0.47, 0.80), 28: (0.53, 0.80),  # ankles
}  # fmt: skip


def make_series(
    seconds: float = 6,
    fps: float = 15,
    run_speed: float = 0.0,
    bounce: float = 0.0,
    knee_bend: float = 0.0,
    visibility: float = 0.95,
    brightness: float = 120,
    missing: slice | None = None,
) -> PoseSeries:
    times = np.arange(0, seconds, 1 / fps)
    image = np.zeros((len(times), 33, 4))
    world = np.zeros((len(times), 33, 3))
    dx = run_speed * np.sin(2 * math.pi * 0.5 * times)  # side-to-side shuttle
    dy = bounce * np.abs(np.sin(2 * math.pi * 1.0 * times))
    limb = knee_bend * np.sin(2 * math.pi * 1.5 * times)
    for idx, (x, y) in BASE.items():
        swing = limb if idx in (13, 14, 15, 16, 25, 26, 27, 28) else 0 * limb
        image[:, idx, 0] = x + dx + swing
        image[:, idx, 1] = y - dy
        image[:, idx, 3] = visibility
        world[:, idx, 0] = x - 0.5 + swing
        world[:, idx, 1] = y - 0.5
    if missing is not None:
        image[missing] = np.nan
        world[missing] = np.nan
    return PoseSeries(
        times=times,
        image=image,
        world=world,
        brightness=np.full(len(times), brightness),
        width=720,
        height=1280,
        crowded_frames=0,
    )


def test_quality_accepts_clear_full_body_clip() -> None:
    q = quality.check(make_series())
    assert q.ok, q.reason
    assert q.stats["detected_ratio"] == 1.0


@pytest.mark.parametrize(
    ("series", "message"),
    [
        (make_series(seconds=1), "too short"),
        (make_series(missing=slice(0, 60)), "couldn't see an athlete"),
        (make_series(missing=slice(0, 60), brightness=20), "too dark"),
        (make_series(visibility=0.3), "whole body"),
    ],
)
def test_quality_rejects_with_actionable_reason(series: PoseSeries, message: str) -> None:
    q = quality.check(series)
    assert not q.ok
    assert message in q.reason


def test_dim_clip_is_fine_when_athlete_is_visible() -> None:
    assert quality.check(make_series(brightness=25)).ok


def test_more_movement_means_higher_speed_and_work_rate() -> None:
    still = metrics.compute(make_series())
    moving = metrics.compute(make_series(run_speed=0.15, bounce=0.05, knee_bend=0.05))
    assert moving["speed"] > still["speed"]
    assert moving["agility"] > still["agility"]
    assert moving["explosiveness"] > still["explosiveness"]
    assert moving["work_rate"] > still["work_rate"]
    assert moving["mobility"] > still["mobility"]


def test_short_gaps_are_bridged() -> None:
    gappy = metrics.compute(make_series(run_speed=0.15, missing=slice(30, 33)))
    full = metrics.compute(make_series(run_speed=0.15))
    assert gappy["speed"] == pytest.approx(full["speed"], rel=0.15)


def test_symmetric_figure_scores_as_symmetric() -> None:
    m = metrics.compute(make_series(knee_bend=0.05))
    assert m["symmetry"] > 0.9


def test_scoring_is_monotonic_and_respects_direction() -> None:
    assert scoring.skill_score("speed", 2.0) > scoring.skill_score("speed", 1.0)
    assert scoring.skill_score("balance", 0.05) > scoring.skill_score("balance", 0.3)
    assert scoring.skill_score("speed", scoring.PRIORS["speed"].mid) == 50
    assert scoring.skill_score("speed", math.nan) is None


def test_overall_needs_enough_skills() -> None:
    scores, overall = scoring.score({"speed": 1.2, "agility": 4.5})
    assert overall is None and set(scores) == {"speed", "agility"}
    full = {name: prior.mid for name, prior in scoring.PRIORS.items()}
    scores, overall = scoring.score(full)
    assert overall == 5.0 and len(scores) == len(scoring.PRIORS)


def test_one_sided_movement_lowers_symmetry() -> None:
    lopsided = make_series(knee_bend=0.05)
    lopsided.world[:, [14, 16], 0] = lopsided.world[0, [14, 16], 0]  # right arm frozen
    assert (
        metrics.compute(lopsided)["symmetry"]
        < metrics.compute(make_series(knee_bend=0.05))["symmetry"]
    )
