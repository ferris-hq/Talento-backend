"""The real MediaPipe model on real files. Needs models/pose_landmarker_full.task and ffmpeg.

Set POSE_SAMPLE_VIDEO to a clip of one athlete (full body in frame) to also check a rating.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from worker.pose import analyze
from worker.pose.landmarker import MODEL_PATH

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists() or shutil.which("ffmpeg") is None, reason="needs pose model and ffmpeg"
)


def test_clip_without_a_person_is_unrated(tmp_path: Path) -> None:
    clip = tmp_path / "empty.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30",
         "-t", "3", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )  # fmt: skip
    result = analyze.run(clip, 3)
    assert result.status == "unrated"
    assert "couldn't see an athlete" in result.reason
    assert result.quality["frames"] >= 40


@pytest.mark.skipif(not os.environ.get("POSE_SAMPLE_VIDEO"), reason="POSE_SAMPLE_VIDEO not set")
def test_sample_clip_is_rated() -> None:
    clip = Path(os.environ["POSE_SAMPLE_VIDEO"])
    result = analyze.run(clip, 10)
    assert result.status == "done", result.reason
    assert 0 < result.overall <= 10
    assert len(result.scores) >= 5
