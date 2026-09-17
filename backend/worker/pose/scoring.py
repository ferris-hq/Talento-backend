"""Map raw metrics to 0-100 skill scores and an overall 0-10 rating.

v1 centres each skill on the typical value from our reference clips (logistic curve), so 50
means "about typical". Once enough athletes are rated, peer percentiles per sport and age
group replace these priors; MODEL_VERSION lets old analyses be re-scored from stored metrics.
"""

import math
from dataclasses import dataclass

MODEL_VERSION = "pose-v1"
MIN_SKILLS = 5


@dataclass(frozen=True)
class Prior:
    mid: float
    scale: float
    higher_is_better: bool = True


# Calibrated on transcoded reference clips (what the worker analyses): mid = median and
# scale ~ interquartile range / 1.5, so the middle half of those clips lands between ~33 and ~67.
PRIORS: dict[str, Prior] = {
    "speed": Prior(1.2, 0.6),  # hip speed p90, torso lengths/s
    "agility": Prior(4.3, 2.3),  # horizontal acceleration p90, torso lengths/s²
    "explosiveness": Prior(1.1, 0.5),  # upward hip velocity p97
    "balance": Prior(0.14, 0.055, higher_is_better=False),  # hip sway over the feet when still
    "coordination": Prior(47.0, 5.0, higher_is_better=False),  # limb jerk / speed
    "symmetry": Prior(0.85, 0.035),  # 1 - left/right range difference
    "mobility": Prior(72.0, 18.0),  # knee/hip/shoulder range, degrees
    "work_rate": Prior(0.82, 0.13),  # share of time moving
}

SKILL_LABELS = {
    "speed": "Speed",
    "agility": "Agility",
    "explosiveness": "Explosiveness",
    "balance": "Balance",
    "coordination": "Coordination",
    "symmetry": "Symmetry",
    "mobility": "Mobility",
    "work_rate": "Work rate",
}


def skill_score(name: str, value: float) -> int | None:
    if value is None or math.isnan(value):
        return None
    prior = PRIORS[name]
    z = (value - prior.mid) / prior.scale
    if not prior.higher_is_better:
        z = -z
    # Never 0 or 100: the curve only says how this clip compares with typical ones.
    return min(99, max(1, round(100 / (1 + math.exp(-z)))))


def score(metrics: dict[str, float]) -> tuple[dict[str, int], float | None]:
    scores = {
        name: s
        for name in PRIORS
        if (s := skill_score(name, metrics.get(name, math.nan))) is not None
    }
    if len(scores) < MIN_SKILLS:
        return scores, None
    overall = round(sum(scores.values()) / len(scores) / 10, 1)
    return scores, overall
