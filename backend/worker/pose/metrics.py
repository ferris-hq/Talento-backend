"""Raw movement metrics from a pose series. Distances are in torso lengths so the camera
distance doesn't matter; angles in degrees. Camera panning inflates body-speed metrics,
so steady shots rate most fairly."""

import warnings
from itertools import pairwise

import numpy as np
from scipy.signal import savgol_filter

from worker.pose.landmarker import PoseSeries
from worker.pose.quality import full_body_mask

L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST = 11, 12, 13, 14, 15, 16
L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE = 23, 24, 25, 26, 27, 28
ENDPOINTS = (L_WRIST, R_WRIST, L_ANKLE, R_ANKLE)
MAX_GAP_S = 0.35
MOVING_BODY = 0.6  # torso lengths / s
MOVING_LIMB = 1.5
STILL_BODY = 0.4


def _fill_gaps(values: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Linearly bridge short detection gaps; longer gaps stay NaN."""
    out = values.copy()
    valid = ~np.isnan(values)
    if valid.sum() < 2:
        return out
    idx = np.flatnonzero(valid)
    for a, b in pairwise(idx):
        if 1 < b - a and times[b] - times[a] <= MAX_GAP_S:
            out[a + 1 : b] = np.interp(
                times[a + 1 : b], [times[a], times[b]], [values[a], values[b]]
            )
    return out


def _smooth(values: np.ndarray) -> np.ndarray:
    """Savitzky-Golay per contiguous run; runs too short to smooth are dropped."""
    out = np.full_like(values, np.nan)
    valid = ~np.isnan(values)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], valid.astype(int), [0]))))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        if stop - start >= 7:
            out[start:stop] = savgol_filter(values[start:stop], 7, 2)
    return out


def _derivative(values: np.ndarray, dt: float) -> np.ndarray:
    return np.gradient(values, dt) if len(values) > 1 else np.full_like(values, np.nan)


def _pct(values: np.ndarray, q: float) -> float:
    values = values[~np.isnan(values)]
    return float(np.percentile(values, q)) if values.size else float("nan")


def _angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Angle at b (degrees) for (T, 3) point series."""
    ba, bc = a - b, c - b
    cos = np.einsum("ij,ij->i", ba, bc) / (np.linalg.norm(ba, axis=1) * np.linalg.norm(bc, axis=1))
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))


def _range(values: np.ndarray) -> float:
    return _pct(values, 95) - _pct(values, 5)


def compute(series: PoseSeries) -> dict[str, float]:
    # Gaps legitimately produce all-NaN slices; they're handled as missing values.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return _compute(series)


def _compute(series: PoseSeries) -> dict[str, float]:
    times = series.times
    dt = 1 / series.fps
    body = full_body_mask(series)

    # Image coordinates in pixels, scaled by the athlete's median torso length.
    px = series.image[:, :, :2] * np.array([series.width, series.height])
    shoulder_mid = (px[:, L_SHOULDER] + px[:, R_SHOULDER]) / 2
    hip_mid = (px[:, L_HIP] + px[:, R_HIP]) / 2
    torso = float(np.nanmedian(np.linalg.norm(shoulder_mid - hip_mid, axis=1)))
    norm = px / torso

    def track(point: np.ndarray) -> np.ndarray:
        return np.stack([_smooth(_fill_gaps(point[:, k], times)) for k in range(2)], axis=1)

    hip = track((norm[:, L_HIP] + norm[:, R_HIP]) / 2)
    hip_v = np.stack([_derivative(hip[:, k], dt) for k in range(2)], axis=1)
    hip_a = np.stack([_derivative(hip_v[:, k], dt) for k in range(2)], axis=1)
    hip_speed = np.linalg.norm(hip_v, axis=1)

    limb_speed, jerk_ratios = [], []
    for joint in ENDPOINTS:
        p = track(norm[:, joint])
        v = np.stack([_derivative(p[:, k], dt) for k in range(2)], axis=1)
        a = np.stack([_derivative(v[:, k], dt) for k in range(2)], axis=1)
        j = np.stack([_derivative(a[:, k], dt) for k in range(2)], axis=1)
        speed = np.linalg.norm(v, axis=1)
        limb_speed.append(speed)
        moving = speed > MOVING_LIMB
        if moving.sum() >= 5:
            jerk = np.linalg.norm(j, axis=1)
            jerk_ratios.append(float(np.nanmean(jerk[moving]) / np.nanmean(speed[moving])))
    limb_speed_max = np.nanmax(np.stack(limb_speed), axis=0)

    # Balance: in still moments, how far the hips drift from over the feet.
    ankle_mid = track((norm[:, L_ANKLE] + norm[:, R_ANKLE]) / 2)
    still = (hip_speed < STILL_BODY) & body
    offset = hip[:, 0] - ankle_mid[:, 0]
    sway = float(np.nanstd(offset[still])) if still.sum() >= 10 else float("nan")

    # Joint ranges from world landmarks (3-D, metres) where the body is visible.
    w = series.world[body]
    rom = {}
    if len(w) >= 10:
        rom = {
            "knee_l": _range(_angle(w[:, L_HIP], w[:, L_KNEE], w[:, L_ANKLE])),
            "knee_r": _range(_angle(w[:, R_HIP], w[:, R_KNEE], w[:, R_ANKLE])),
            "hip_l": _range(_angle(w[:, L_SHOULDER], w[:, L_HIP], w[:, L_KNEE])),
            "hip_r": _range(_angle(w[:, R_SHOULDER], w[:, R_HIP], w[:, R_KNEE])),
            "elbow_l": _range(_angle(w[:, L_SHOULDER], w[:, L_ELBOW], w[:, L_WRIST])),
            "elbow_r": _range(_angle(w[:, R_SHOULDER], w[:, R_ELBOW], w[:, R_WRIST])),
            "shoulder_l": _range(_angle(w[:, L_HIP], w[:, L_SHOULDER], w[:, L_ELBOW])),
            "shoulder_r": _range(_angle(w[:, R_HIP], w[:, R_SHOULDER], w[:, R_ELBOW])),
        }
    pairs = [
        ("knee_l", "knee_r"),
        ("hip_l", "hip_r"),
        ("elbow_l", "elbow_r"),
        ("shoulder_l", "shoulder_r"),
    ]
    asym = [abs(rom[a] - rom[b]) / max(rom[a], rom[b], 10.0) for a, b in pairs if a in rom]
    mobility = [
        rom[k]
        for k in ("knee_l", "knee_r", "hip_l", "hip_r", "shoulder_l", "shoulder_r")
        if k in rom
    ]

    observed = ~np.isnan(hip_speed)
    moving = (hip_speed > MOVING_BODY) | (limb_speed_max > MOVING_LIMB)

    return {
        "speed": _pct(hip_speed, 90),
        "agility": _pct(np.abs(hip_a[:, 0]), 90),
        "explosiveness": _pct(-hip_v[:, 1], 97),
        "balance": sway,
        "coordination": float(np.median(jerk_ratios)) if jerk_ratios else float("nan"),
        "symmetry": 1 - float(np.mean(asym)) if asym else float("nan"),
        "mobility": float(np.mean(mobility)) if mobility else float("nan"),
        "work_rate": float(moving[observed].mean()) if observed.any() else float("nan"),
    }
