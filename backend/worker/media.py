"""ffprobe / ffmpeg helpers. Pure functions over local files so they can be tested without R2."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

MIN_SECONDS = 2.0
TARGET_SHORT_SIDE = 720


class UnusableVideo(Exception):
    """The clip can't be used; the message is shown to the athlete."""


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    width: int
    height: int
    rotation: int
    has_audio: bool

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation % 180:
            return self.height, self.width
        return self.width, self.height


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def probe(path: Path) -> ProbeResult:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise UnusableVideo("We couldn't read this file. Try exporting it as MP4 and upload again.")
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise UnusableVideo("This file has no video in it.")

    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    rotation = int(video.get("tags", {}).get("rotate", rotation) or 0)

    return ProbeResult(
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        rotation=abs(rotation) % 360,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def validate(result: ProbeResult, max_seconds: int) -> None:
    if result.duration_s < MIN_SECONDS:
        raise UnusableVideo(f"Clips need to be at least {int(MIN_SECONDS)} seconds long.")
    if result.duration_s > max_seconds + 1:
        raise UnusableVideo(
            f"Clips can be at most {max_seconds} seconds. Trim it and upload again."
        )
    if min(result.width, result.height) < 240:
        raise UnusableVideo("The video resolution is too low. Record at 480p or higher.")


def _scale_filter() -> str:
    # Shrink so the short side is at most 720 (never upscale); keep even dimensions for H.264.
    s = TARGET_SHORT_SIDE
    return f"scale='if(gt(iw,ih),-2,min({s},iw))':'if(gt(iw,ih),min({s},ih),-2)',setsar=1"


def transcode(src: Path, dest: Path, probe_result: ProbeResult, threads: int = 2) -> None:
    args = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-map",
        "0:v:0",
    ]
    if probe_result.has_audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        args += ["-an"]
    args += [
        "-vf",
        _scale_filter(),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-maxrate",
        "2500k",
        "-bufsize",
        "5000k",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-movflags",
        "+faststart",
        "-threads",
        str(threads),
        str(dest),
    ]
    result = _run(args, timeout=15 * 60)
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg transcode failed: {result.stderr[-500:]}")


def poster(src: Path, dest: Path, duration_s: float) -> None:
    at = min(1.0, max(duration_s / 2, 0))
    result = _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{at:.2f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(720,iw)':-2",
            "-q:v",
            "4",
            str(dest),
        ],
        timeout=120,
    )
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg poster failed: {result.stderr[-500:]}")
