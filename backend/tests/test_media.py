import shutil
import subprocess
from pathlib import Path

import pytest

from worker import media

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_clip(path: Path, *, seconds: float, size: str, audio: bool = True) -> Path:
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    subprocess.run(args, check=True)
    return path


def test_portrait_clip_is_transcoded_to_720_short_side(tmp_path: Path) -> None:
    src = make_clip(tmp_path / "in.mov", seconds=3, size="1080x1920")
    info = media.probe(src)
    assert (info.width, info.height, info.has_audio) == (1080, 1920, True)
    media.validate(info, max_seconds=90)

    out = tmp_path / "out.mp4"
    media.transcode(src, out, info)
    media.poster(out, tmp_path / "poster.jpg", info.duration_s)

    result = media.probe(out)
    assert (result.width, result.height) == (720, 1280)
    assert result.has_audio
    assert 2.5 < result.duration_s < 3.5
    assert (tmp_path / "poster.jpg").stat().st_size > 1000


def test_small_landscape_clip_is_not_upscaled(tmp_path: Path) -> None:
    src = make_clip(tmp_path / "in.mp4", seconds=3, size="640x360", audio=False)
    info = media.probe(src)
    out = tmp_path / "out.mp4"
    media.transcode(src, out, info)
    result = media.probe(out)
    assert (result.width, result.height) == (640, 360)
    assert not result.has_audio


@pytest.mark.parametrize(
    ("seconds", "size", "message"),
    [
        (1, "640x360", "at least 2 seconds"),
        (5, "640x360", "at most 3 seconds"),
        (3, "320x180", "resolution is too low"),
    ],
)
def test_unusable_clips_are_rejected(
    tmp_path: Path, seconds: float, size: str, message: str
) -> None:
    src = make_clip(tmp_path / "in.mp4", seconds=seconds, size=size, audio=False)
    with pytest.raises(media.UnusableVideo, match=message):
        media.validate(media.probe(src), max_seconds=3)


def test_non_video_file_is_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "notes.mp4"
    junk.write_text("definitely not a video")
    with pytest.raises(media.UnusableVideo):
        media.probe(junk)
