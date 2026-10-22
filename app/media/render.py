from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

from app.core.config import Settings
from app.media.intake import MediaError, probe_video
from app.models.timeline import Caption, Timeline


def _atempo_filters(speed: float) -> list[str]:
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return [f"atempo={factor:.8f}" for factor in factors]


def _filter_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def _caption_intervals(
    timeline: Timeline,
    caption: Caption,
) -> list[tuple[float, float]]:
    positions = timeline.word_positions()
    if caption.source_start is not None and caption.source_end is not None:
        output: list[tuple[float, float]] = []
        cursor = 0.0
        for segment in timeline.segments:
            start = max(caption.source_start, segment.source_start)
            end = min(caption.source_end, segment.source_end)
            if start < end:
                output.append(
                    (
                        cursor + (start - segment.source_start) / segment.speed,
                        cursor + (end - segment.source_start) / segment.speed,
                    )
                )
            cursor += segment.duration
        return output
    if caption.start_word_id is None or caption.end_word_id is None:
        return []
    starts = positions.get(caption.start_word_id, [])
    ends = positions.get(caption.end_word_id, [])
    pairs = [
        (start[0], end[1])
        for start in starts
        for end in ends
        if start[0] <= end[1]
    ]
    return sorted(pairs)


class FFmpegRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def render(
        self,
        media_path: Path,
        timeline: Timeline,
        output_path: Path,
        has_audio: bool,
    ) -> dict[str, Any]:
        if not timeline.segments:
            raise MediaError("The timeline contains no media to render.")
        if not media_path.is_file():
            raise MediaError("The source video is missing; it was not modified.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command, expected_duration = self._build_command(
            media_path,
            timeline,
            output_path,
            has_audio,
        )
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=max(120, int(expected_duration * 10)),
            )
        except FileNotFoundError as error:
            raise MediaError(
                "FFmpeg is not installed or FFMPEG_PATH is incorrect."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise MediaError("Video rendering exceeded its time limit.") from error
        except subprocess.CalledProcessError as error:
            message = error.stderr.strip()[-1500:] or "FFmpeg could not render the timeline."
            raise MediaError(message) from error
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise MediaError("FFmpeg reported success but produced no output file.")
        output_info = probe_video(output_path, self.settings)
        frame_rate = output_info.get("frame_rate") or 30.0
        expected_frames = expected_duration * frame_rate
        actual_frames = output_info["duration_s"] * frame_rate
        length_difference_frames = abs(expected_frames - actual_frames)
        if length_difference_frames > 1.01:
            raise MediaError(
                "Rendered length differs from the timeline by more than one frame "
                f"({length_difference_frames:.2f} frames)."
            )
        av_offset_frames = (
            float(output_info.get("av_offset_s") or 0.0) * frame_rate
        )
        if av_offset_frames > 1.01:
            raise MediaError(
                "Rendered audio and video start times differ by more than one frame "
                f"({av_offset_frames:.2f} frames)."
            )
        return {
            "path": str(output_path),
            "duration_s": output_info["duration_s"],
            "expected_duration_s": expected_duration,
            "length_difference_frames": length_difference_frames,
            "av_offset_frames": av_offset_frames,
            "frame_rate": frame_rate,
            "stderr_tail": result.stderr[-500:] if result.stderr else "",
        }

    def _build_command(
        self,
        media_path: Path,
        timeline: Timeline,
        output_path: Path,
        has_audio: bool,
    ) -> tuple[list[str], float]:
        command = [
            self.settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
        ]
        music_paths: list[Path] = []
        for track in timeline.music_tracks:
            path = Path(track.source).expanduser()
            if not path.is_file():
                raise MediaError(
                    f"Music source {track.source!r} is not an accessible local file."
                )
            music_paths.append(path)
            command.extend(["-stream_loop", "-1", "-i", str(path)])

        filters: list[str] = []
        audio_labels: list[str] = []
        for index, segment in enumerate(timeline.segments):
            start = segment.source_start
            end = segment.source_end
            speed = segment.speed
            filters.append(
                f"[0:v:0]trim=start={start:.9f}:end={end:.9f},"
                f"setpts=(PTS-STARTPTS)/{speed:.9f},setsar=1[v{index}]"
            )
            if has_audio:
                filters.append(
                    f"[0:a:0]atrim=start={start:.9f}:end={end:.9f},"
                    "asetpts=PTS-STARTPTS,"
                    f"{','.join(_atempo_filters(speed))}[a{index}]"
                )
            else:
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={segment.duration:.9f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            audio_labels.extend([f"[v{index}]", f"[a{index}]"])

        count = len(timeline.segments)
        filters.append(
            f"{''.join(audio_labels)}concat=n={count}:v=1:a=1[vbase][abase]"
        )
        audio_label = "abase"
        cursor = 0.0
        word_positions = timeline.word_positions()
        for effect_index, effect in enumerate(timeline.effects):
            intervals = word_positions.get(effect.word_id, [])
            for interval_index, (start, end) in enumerate(intervals):
                if effect.kind == "mute":
                    next_label = f"amute{effect_index}_{interval_index}"
                    filters.append(
                        f"[{audio_label}]volume=0:enable='between(t,{start:.9f},{end:.9f})'"
                        f"[{next_label}]"
                    )
                    audio_label = next_label
                elif effect.kind == "bleep":
                    muted_label = f"ableepmute{effect_index}_{interval_index}"
                    tone_label = f"ableeptone{effect_index}_{interval_index}"
                    mixed_label = f"ableepmix{effect_index}_{interval_index}"
                    filters.extend(
                        [
                            f"[{audio_label}]volume=0:enable='between(t,{start:.9f},{end:.9f})'"
                            f"[{muted_label}]",
                            f"aevalsrc=if(between(t\\,{start:.9f}\\,{end:.9f})\\,"
                            f"0.15*sin(2*PI*{effect.frequency:.1f}*t)\\,0):"
                            f"s=48000:d={timeline.current_duration:.9f}[{tone_label}]",
                            f"[{muted_label}][{tone_label}]amix=inputs=2:duration=first:"
                            f"normalize=0[{mixed_label}]",
                        ]
                    )
                    audio_label = mixed_label

        for index, track in enumerate(timeline.music_tracks):
            input_index = index + 1
            music_label = f"music{index}"
            filters.append(
                f"[{input_index}:a:0]atrim=duration={timeline.current_duration:.9f},"
                f"asetpts=PTS-STARTPTS,volume={track.volume:.6f}[{music_label}]"
            )
            if track.ducking:
                duck_label = f"ducked{index}"
                filters.append(
                    f"[{music_label}][{audio_label}]sidechaincompress="
                    "threshold=0.04:ratio=8:attack=10:release=250"
                    f"[{duck_label}]"
                )
                music_label = duck_label
            mixed_label = f"amusic{index}"
            filters.append(
                f"[{audio_label}][{music_label}]amix=inputs=2:duration=first:"
                f"normalize=0[{mixed_label}]"
            )
            audio_label = mixed_label

        video_label = "vbase"
        font_file = ""
        if self.settings.ffmpeg_font_file:
            font_path = Path(self.settings.ffmpeg_font_file).expanduser()
            if not font_path.is_file():
                raise MediaError(
                    f"Configured caption font file {str(font_path)!r} does not exist."
                )
            font_value = font_path.resolve().as_posix().replace(":", r"\\:")
            font_file = f"fontfile={font_value}:"
        for index, caption in enumerate(timeline.captions):
            for interval_index, (start, end) in enumerate(
                _caption_intervals(timeline, caption)
            ):
                next_label = f"vcap{index}_{interval_index}"
                filters.append(
                    f"[{video_label}]drawtext={font_file}"
                    f"text='{_filter_text(caption.text)}':"
                    f"fontcolor={caption.color}:fontsize={caption.font_size}:"
                    "x=(w-text_w)/2:y=h-text_h-40:"
                    f"enable='between(t,{start:.9f},{end:.9f})'[{next_label}]"
                )
                video_label = next_label

        filters.extend(
            [
                f"[{video_label}]format=yuv420p[vout]",
                f"[{audio_label}]atrim=duration={timeline.current_duration:.9f},"
                "asetpts=PTS-STARTPTS[aout]",
            ]
        )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                "-t",
                f"{timeline.current_duration:.9f}",
                str(output_path),
            ]
        )
        return command, timeline.current_duration
