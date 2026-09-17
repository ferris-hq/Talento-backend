"""One clip in, one analysis out."""

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from worker.pose import landmarker, metrics, quality, scoring


@dataclass
class Analysis:
    status: str  # "done" | "unrated"
    reason: str | None
    quality: dict
    metrics: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    overall: float | None = None
    processing_ms: int = 0
    model_version: str = scoring.MODEL_VERSION


def _clean(values: dict[str, float]) -> dict[str, float | None]:
    return {k: (None if math.isnan(v) else round(v, 4)) for k, v in values.items()}


def run(video: Path) -> Analysis:
    started = time.monotonic()
    series = landmarker.extract(video)
    q = quality.check(series)
    if not q.ok:
        result = Analysis("unrated", q.reason, q.stats)
    else:
        raw = metrics.compute(series)
        scores, overall = scoring.score(raw)
        if overall is None:
            result = Analysis(
                "unrated",
                "We couldn't measure enough of your movement. Film a longer, steadier clip.",
                q.stats,
                _clean(raw),
                scores,
            )
        else:
            result = Analysis("done", None, q.stats, _clean(raw), scores, overall)
    result.processing_ms = round((time.monotonic() - started) * 1000)
    return result
