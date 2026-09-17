"""Decide whether a clip can be rated, with a reason the athlete can act on."""

from dataclasses import dataclass, field

import numpy as np

from worker.pose.landmarker import PoseSeries

# BlazePose indices
SHOULDERS, HIPS, KNEES, ANKLES = (11, 12), (23, 24), (25, 26), (27, 28)
BODY = (*SHOULDERS, *HIPS, *KNEES, *ANKLES)

MIN_FRAMES = 20
MIN_DETECTED = 0.6
MIN_FULL_BODY = 0.4
MIN_VISIBILITY = 0.5
DARK = 45  # mean luma
VISIBLE = 0.5


@dataclass
class Quality:
    ok: bool
    reason: str | None
    stats: dict = field(default_factory=dict)


def full_body_mask(series: PoseSeries) -> np.ndarray:
    vis = series.image[:, BODY, 3]
    return np.nan_to_num(vis, nan=0.0).min(axis=1) > VISIBLE


def check(series: PoseSeries) -> Quality:
    frames = len(series.times)
    detected = series.detected
    det_ratio = float(detected.mean()) if frames else 0.0
    full_body = float(full_body_mask(series).mean()) if frames else 0.0
    visibility = float(np.nanmean(series.image[detected][:, BODY, 3])) if detected.any() else 0.0
    brightness = float(series.brightness.mean()) if frames else 0.0
    stats = {
        "frames": frames,
        "sample_fps": round(series.fps, 1),
        "detected_ratio": round(det_ratio, 3),
        "full_body_ratio": round(full_body, 3),
        "mean_visibility": round(visibility, 3),
        "brightness": round(brightness, 1),
        "crowded_ratio": round(series.crowded_frames / frames, 3) if frames else 0.0,
    }

    def fail(reason: str) -> Quality:
        return Quality(False, reason, stats)

    if frames < MIN_FRAMES:
        return fail("The clip is too short to rate. Film at least a few seconds of movement.")
    if det_ratio < MIN_DETECTED:
        # Dim footage is fine while the athlete is found; only blame the light when they aren't.
        if brightness < DARK:
            return fail("The clip is too dark to rate. Film in daylight or under floodlights.")
        return fail(
            "We couldn't see an athlete for most of the clip. Keep one player clearly in frame."
        )
    if full_body < MIN_FULL_BODY or visibility < MIN_VISIBILITY:
        return fail("Keep your whole body, head to feet, in frame so we can rate your movement.")
    return Quality(True, None, stats)
