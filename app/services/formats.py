"""yt-dlp formats -> download options shown in the UI"""

from dataclasses import dataclass, field
from typing import Any

# no rtmp etc: those go through ffmpeg and bypass the socket guard
SAFE_PROTOCOLS = {"http", "https", "m3u8_native", "m3u8", "http_dash_segments", "http_dash_segments_generator"}

STANDARD_HEIGHTS = [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]
MP3_BITRATE_KBPS = 192

QUALITY_NAMES = {2160: "4K", 4320: "8K", 1440: "2K"}


@dataclass
class DownloadOption:
    id: str
    kind: str  # "video" | "audio"
    quality: str  # "best" | "<height>" | "audio"
    container: str  # "original" | "mp4" | "mp3"
    label: str
    detail: str
    ext: str | None
    size: int | None = None
    size_is_estimate: bool = True
    needs_ffmpeg: bool = False
    recipe: dict[str, Any] = field(default_factory=dict)  # yt-dlp params, never sent to the client

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "quality": self.quality,
            "container": self.container,
            "label": self.label,
            "detail": self.detail,
            "ext": self.ext,
            "size": self.size,
            "size_is_estimate": self.size_is_estimate,
            "needs_ffmpeg": self.needs_ffmpeg,
        }


def _codec(value: Any) -> str | None:
    # "none" = no stream, None = unknown
    if value is None:
        return None
    value = str(value).lower()
    return "none" if value == "none" else value


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def normalise_formats(raw_formats: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for f in raw_formats or []:
        if not isinstance(f, dict):
            continue
        protocol = str(f.get("protocol") or "https").lower()
        ext = str(f.get("ext") or "").lower() or None
        note = str(f.get("format_note") or "").lower()
        if ext in {"mhtml", "jpg", "png", "webp"} or "storyboard" in note:
            continue
        if any(p not in SAFE_PROTOCOLS for p in protocol.split("+")):
            continue
        if f.get("has_drm"):
            continue
        vcodec, acodec = _codec(f.get("vcodec")), _codec(f.get("acodec"))
        if vcodec == "none" and acodec == "none":
            continue
        height = f.get("height")
        height = int(height) if isinstance(height, (int, float)) and height > 0 else None
        result.append(
            {
                "format_id": str(f.get("format_id") or ""),
                "ext": ext,
                "vcodec": vcodec,
                "acodec": acodec,
                "height": height,
                "tbr": _num(f.get("tbr")),
                "abr": _num(f.get("abr")),
                "filesize": int(f["filesize"]) if _num(f.get("filesize")) else None,
                "filesize_approx": int(f["filesize_approx"]) if _num(f.get("filesize_approx")) else None,
            }
        )
    return result


def bucket_height(height: int) -> int:
    for step in STANDARD_HEIGHTS:
        if height <= step * 1.05:
            return step
    return STANDARD_HEIGHTS[-1]


def quality_label(height: int) -> str:
    name = QUALITY_NAMES.get(height)
    return f"{height}p · {name}" if name else f"{height}p"


def _size(f) -> tuple[int | None, bool]:
    if f.get("filesize"):
        return f["filesize"], False
    if f.get("filesize_approx"):
        return f["filesize_approx"], True
    return None, True


VIDEO_CODEC_NAMES = [
    ("avc", "H.264"), ("h264", "H.264"), ("hvc", "H.265"), ("hev", "H.265"), ("h265", "H.265"),
    ("vp09", "VP9"), ("vp9", "VP9"), ("vp8", "VP8"), ("av01", "AV1"), ("av1", "AV1"),
]
AUDIO_CODEC_NAMES = [("mp4a", "AAC"), ("aac", "AAC"), ("opus", "Opus"), ("vorbis", "Vorbis"), ("mp3", "MP3"), ("flac", "FLAC")]


def codec_name(codec: str | None, table) -> str | None:
    if not codec or codec == "none":
        return None
    for prefix, name in table:
        if codec.startswith(prefix):
            return name
    return None


def _merged_ext(video: dict, audio: dict | None) -> str:
    # roughly what yt-dlp picks when merging without merge_output_format
    if audio is None:
        return video.get("ext") or "mp4"
    v, a = video.get("ext"), audio.get("ext")
    if v == "mp4" and a in {"m4a", "mp4"}:
        return "mp4"
    if v == "webm" and a in {"webm", "weba"}:
        return "webm"
    return "mkv"


def _join(*parts: str | None) -> str:
    return " · ".join(p for p in parts if p)


def _estimate(parts: list[dict | None]) -> tuple[int | None, bool]:
    total, approx = 0, False
    for part in parts:
        if part is None:
            continue
        size, is_approx = _size(part)
        if size is None:
            return None, True
        total += size
        approx = approx or is_approx
    return (total or None), approx


AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "ogg", "oga", "weba", "flac", "wav", "mka"}
MP4_SORT = ["res", "ext:mp4:m4a"]
AUDIO_EXT_BY_CODEC = {"AAC": "m4a", "Opus": "opus", "MP3": "mp3", "Vorbis": "ogg", "FLAC": "flac"}


def stream_kind(f: dict) -> str:
    # a / v / av
    if f["vcodec"] == "none":
        return "a"
    if f["acodec"] == "none":
        return "v"
    if f["vcodec"] is None and f.get("ext") in AUDIO_EXTS:
        return "a"
    return "av"


def _best_audio(audios: list[dict], prefer_ext: str | None = None) -> dict | None:
    if not audios:
        return None
    return max(audios, key=lambda f: (bool(prefer_ext) and f.get("ext") == prefer_ext, f.get("abr") or f.get("tbr") or 0))


def _best_video(videos: list[dict], prefer_ext: str | None = None) -> dict | None:
    if not videos:
        return None
    return max(
        videos,
        key=lambda f: (f.get("height") or 0, bool(prefer_ext) and f.get("ext") == prefer_ext, f.get("tbr") or 0),
    )


def _video_tiers(streams: list[dict]) -> list[tuple[str, int | None, int]]:
    # (quality, max_height, lower_bound)
    buckets: dict[int, int] = {}
    for f in streams:
        if f["height"]:
            b = bucket_height(f["height"])
            buckets[b] = max(buckets.get(b, 0), f["height"])
    ordered = sorted(buckets.items(), reverse=True)
    tiers: list[tuple[str, int | None, int]] = [("best", None, 0)] if streams else []
    for idx, (bucket, raw_max) in enumerate(ordered):
        if idx == 0:
            # top one is the same as "best"
            continue
        lower = ordered[idx + 1][1] if idx + 1 < len(ordered) else 0
        tiers.append((str(bucket), raw_max, lower))
    return tiers


def _video_option(quality, max_h, lower, container, progressive, video_only, audio_only, has_ffmpeg):
    def in_tier(f):
        return max_h is None or (f["height"] is not None and lower < f["height"] <= max_h)

    tier_prog = [f for f in progressive if in_tier(f)]
    tier_vonly = [f for f in video_only if in_tier(f)]
    h = f"[height<={max_h}][height>{lower}]" if max_h is not None else ""
    prefer = "mp4" if container == "mp4" else None

    if has_ffmpeg:
        video = _best_video(tier_vonly + tier_prog, prefer)
        if video is None:
            return None
        audio = _best_audio(audio_only, "m4a" if prefer else None) if stream_kind(video) == "v" else None
        selector = f"bv*{h}+ba/b{h}"
        if max_h is not None:
            selector += f"/bv*[height<={max_h}]+ba/b[height<={max_h}]"
        recipe: dict[str, Any] = {"format": selector}
        if container == "mp4":
            # remux only, no re-encoding
            recipe.update({"format_sort": MP4_SORT, "merge_output_format": "mp4", "remux": "mp4"})
    else:
        pool = [f for f in tier_prog if container != "mp4" or f.get("ext") == "mp4"]
        video, audio = _best_video(pool, prefer), None
        if video is None:
            return None
        recipe = {"format": f"b{h}" + ("[ext=mp4]" if container == "mp4" else "")}

    size, approx = _estimate([video, audio])
    ext = "mp4" if container == "mp4" else _merged_ext(video, audio)
    vname = codec_name(video.get("vcodec"), VIDEO_CODEC_NAMES)
    has_sound = audio is not None or stream_kind(video) == "av"
    aname = codec_name((audio or video).get("acodec"), AUDIO_CODEC_NAMES) if has_sound else "без звука"
    resolution = f"{video['height']}p" if quality == "best" and video.get("height") else None
    return DownloadOption(
        id=f"video-{quality}-{container}",
        kind="video",
        quality=quality,
        container=container,
        label="Лучшее качество" if quality == "best" else quality_label(int(quality)),
        detail=_join(resolution, ext.upper(), vname, aname),
        ext=ext,
        size=size,
        size_is_estimate=approx,
        needs_ffmpeg=has_ffmpeg and (audio is not None or container == "mp4"),
        recipe=recipe,
    )


def _audio_options(progressive, audio_only, has_ffmpeg, duration) -> list[DownloadOption]:
    options: list[DownloadOption] = []
    if not (audio_only or progressive):
        return options
    if has_ffmpeg:
        # rough estimate: duration * bitrate
        size = int(duration * MP3_BITRATE_KBPS * 1000 / 8) if duration else None
        options.append(DownloadOption(
            id="audio-mp3", kind="audio", quality="audio", container="mp3",
            label="MP3", detail=f"MP3 · {MP3_BITRATE_KBPS} кбит/с", ext="mp3",
            size=size, size_is_estimate=True, needs_ffmpeg=True,
            recipe={"format": "ba/b", "extract_audio": "mp3", "audio_quality": str(MP3_BITRATE_KBPS)},
        ))
    if audio_only:
        best = _best_audio(audio_only)
        size, approx = _size(best)
        abr = f"{round(best['abr'])} кбит/с" if best.get("abr") else None
        ext = best.get("ext")
        options.append(DownloadOption(
            id="audio-original", kind="audio", quality="audio", container="original",
            label="Аудио в исходном формате",
            detail=_join((ext or "").upper() or None, codec_name(best.get("acodec"), AUDIO_CODEC_NAMES), abr),
            ext=ext, size=size, size_is_estimate=approx, needs_ffmpeg=False,
            recipe={"format": "ba"},
        ))
    elif has_ffmpeg:
        # only muxed streams, pull the audio track out
        best = _best_video(progressive)
        aname = codec_name(best.get("acodec"), AUDIO_CODEC_NAMES)
        options.append(DownloadOption(
            id="audio-original", kind="audio", quality="audio", container="original",
            label="Аудио в исходном формате", detail=_join(aname, "дорожка из видео"),
            ext=AUDIO_EXT_BY_CODEC.get(aname or ""), size=None, needs_ffmpeg=True,
            recipe={"format": "b", "extract_audio": "best"},
        ))
    return options


def build_options(
    raw_formats: list[dict[str, Any]] | None,
    *,
    has_ffmpeg: bool,
    duration: float | None = None,
    max_filesize: int | None = None,
) -> list[DownloadOption]:
    fmts = normalise_formats(raw_formats)
    audio_only = [f for f in fmts if stream_kind(f) == "a"]
    video_only = [f for f in fmts if stream_kind(f) == "v"]
    progressive = [f for f in fmts if stream_kind(f) == "av"]

    options: list[DownloadOption] = []
    usable_video = video_only + progressive if has_ffmpeg else progressive
    for quality, max_h, lower in _video_tiers(usable_video):
        for container in ("mp4", "original"):
            opt = _video_option(quality, max_h, lower, container, progressive, video_only, audio_only, has_ffmpeg)
            if opt is None:
                continue
            # source is mp4 already -> same as the mp4 option
            if container == "original" and opt.ext == "mp4" and any(
                o.quality == quality and o.container == "mp4" for o in options
            ):
                continue
            options.append(opt)

    options.extend(_audio_options(progressive, audio_only, has_ffmpeg, duration))

    if max_filesize:
        options = [o for o in options if not (o.size and not o.size_is_estimate and o.size > max_filesize)]
    return options

