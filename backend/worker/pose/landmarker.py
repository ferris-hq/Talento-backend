"""Run MediaPipe PoseLandmarker over a video and keep one athlete's landmark series."""

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

MODEL_PATH = Path(
    os.environ.get(
        "POSE_MODEL_PATH",
        Path(__file__).resolve().parents[2] / "models" / "pose_landmarker_full.task",
    )
)
MAX_POSES = 3  # a few people may be in frame; we follow one of them
# Metrics use derivatives, which depend on the sampling rate, so every clip is analysed on the
# same fixed timeline whatever its source frame rate.
ANALYSIS_FPS = 12.0
MAX_BLEND_GAP_S = 0.2


@dataclass
class PoseSeries:
    """Landmarks for the tracked athlete; frames without a detection are NaN."""

    times: np.ndarray  # (T,) seconds
    image: np.ndarray  # (T, 33, 4) x, y in [0,1] image space, z, visibility
    world: np.ndarray  # (T, 33, 3) metres, hip-centred
    brightness: np.ndarray  # (T,) mean luma 0-255
    width: int
    height: int
    crowded_frames: int  # frames with more than one person detected

    @property
    def fps(self) -> float:
        return (
            (len(self.times) - 1) / (self.times[-1] - self.times[0]) if len(self.times) > 1 else 0
        )

    @property
    def detected(self) -> np.ndarray:
        return ~np.isnan(self.image[:, 0, 0])


def _resample(times: np.ndarray, values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Linear interpolation onto `grid` between detected neighbours; NaN across long gaps."""
    out = np.full((len(grid), *values.shape[1:]), np.nan)
    right = np.clip(np.searchsorted(times, grid), 1, len(times) - 1)
    left = right - 1
    t0, t1 = times[left], times[right]
    w = np.clip((grid - t0) / np.maximum(t1 - t0, 1e-9), 0, 1)
    v0, v1 = values[left], values[right]
    shape = (-1,) + (1,) * (values.ndim - 1)
    blended = v0 * (1 - w).reshape(shape) + v1 * w.reshape(shape)
    ok = (t1 - t0 <= MAX_BLEND_GAP_S).reshape(shape)
    out[:] = np.where(ok, blended, np.nan)
    return out


def _bbox(landmarks) -> tuple[float, float, float, float]:
    xs = [p.x for p in landmarks]
    ys = [p.y for p in landmarks]
    return min(xs), min(ys), max(xs), max(ys)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _pick(result, previous_box):
    """Follow the same person between frames; start with the largest one."""
    poses = result.pose_landmarks
    if not poses:
        return None
    boxes = [_bbox(p) for p in poses]
    if previous_box is not None:
        overlaps = [_iou(b, previous_box) for b in boxes]
        best = int(np.argmax(overlaps))
        if overlaps[best] > 0.1:
            return best
    return int(np.argmax([_area(b) for b in boxes]))


def extract(video: Path, model_path: Path = MODEL_PATH) -> PoseSeries:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"cannot open {video}")
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # Sample at least ANALYSIS_FPS, then resample exactly onto the analysis timeline.
    step = max(1, int(source_fps // ANALYSIS_FPS))

    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(
            model_asset_path=str(model_path), delegate=BaseOptions.Delegate.CPU
        ),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=MAX_POSES,
    )
    times: list[float] = []
    image_rows: list[np.ndarray] = []
    world_rows: list[np.ndarray] = []
    brightness: list[float] = []
    crowded = 0
    previous_box = None
    nan_image = np.full((33, 4), np.nan)
    nan_world = np.full((33, 3), np.nan)

    with vision.PoseLandmarker.create_from_options(options) as landmarker:
        index = 0
        while True:
            ok = capture.grab()
            if not ok:
                break
            if index % step == 0:
                ok, frame = capture.retrieve()
                if not ok:
                    break
                timestamp_ms = round(index * 1000 / source_fps)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp_ms
                )
                brightness.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()))
                times.append(timestamp_ms / 1000)
                if len(result.pose_landmarks) > 1:
                    crowded += 1
                chosen = _pick(result, previous_box)
                if chosen is None:
                    image_rows.append(nan_image)
                    world_rows.append(nan_world)
                else:
                    pose = result.pose_landmarks[chosen]
                    previous_box = _bbox(pose)
                    image_rows.append(
                        np.array([[p.x, p.y, p.z, p.visibility or 0.0] for p in pose])
                    )
                    world = result.pose_world_landmarks[chosen]
                    world_rows.append(np.array([[p.x, p.y, p.z] for p in world]))
            index += 1
    capture.release()

    if len(times) < 2:
        return PoseSeries(
            times=np.array(times),
            image=np.array(image_rows).reshape(-1, 33, 4),
            world=np.array(world_rows).reshape(-1, 33, 3),
            brightness=np.array(brightness),
            width=width,
            height=height,
            crowded_frames=crowded,
        )
    t = np.array(times)
    grid = np.arange(t[0], t[-1] + 1e-9, 1 / ANALYSIS_FPS)
    crowded_share = crowded / len(t)
    return PoseSeries(
        times=grid,
        image=_resample(t, np.array(image_rows), grid),
        world=_resample(t, np.array(world_rows), grid),
        brightness=_resample(t, np.array(brightness), grid),
        width=width,
        height=height,
        crowded_frames=round(crowded_share * len(grid)),
    )
