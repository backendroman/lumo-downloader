from app.services.formats import build_options, bucket_height, normalise_formats
from tests.conftest import SAMPLE_FORMATS


def ids(options):
    return [o.id for o in options]


def test_options_with_ffmpeg_only_real_resolutions():
    options = build_options(SAMPLE_FORMATS, has_ffmpeg=True, duration=212)
    got = ids(options)
    assert "video-best-mp4" in got
    # 1080 == best
    assert "video-1080-mp4" not in got
    best = next(o for o in options if o.id == "video-best-mp4")
    assert best.detail.startswith("1080p")
    assert "video-720-mp4" in got and "video-360-mp4" in got
    # 2160 is rtmp only
    assert not any("2160" in i for i in got)
    assert not any(q in i for i in got for q in ("480", "240", "1440"))
    assert "audio-mp3" in got and "audio-original" in got


def test_original_option_hidden_when_it_equals_mp4():
    options = build_options(SAMPLE_FORMATS, has_ffmpeg=True)
    by_id = {o.id: o for o in options}
    # best is vp9/webm, so original != mp4
    assert "video-best-original" in by_id
    assert by_id["video-best-original"].ext in {"webm", "mkv"}
    assert by_id["video-best-mp4"].ext == "mp4"
    # 360p is already h264 mp4
    assert "video-360-original" not in by_id


def test_recipes_are_server_side_and_constrained():
    for opt in build_options(SAMPLE_FORMATS, has_ffmpeg=True, duration=100):
        public = opt.public()
        assert "recipe" not in public
        assert "format" not in public
        if opt.quality.isdigit():
            assert f"[height<={opt.quality}]" in opt.recipe["format"]
        elif opt.kind == "video":
            assert "height" not in opt.recipe["format"]
    mp3 = next(o for o in build_options(SAMPLE_FORMATS, has_ffmpeg=True, duration=100) if o.id == "audio-mp3")
    assert mp3.recipe["extract_audio"] == "mp3"


def test_without_ffmpeg_only_progressive_formats():
    options = build_options(SAMPLE_FORMATS, has_ffmpeg=False)
    got = ids(options)
    # without ffmpeg only format 18 works
    assert "video-best-mp4" in got
    best = next(o for o in options if o.id == "video-best-mp4")
    assert best.detail.startswith("360p")
    assert not any(i.startswith(("video-1080", "video-720")) for i in got)
    assert "audio-mp3" not in got
    assert "audio-original" in got
    assert all(not o.needs_ffmpeg for o in options)


def test_audio_only_source():
    fmts = [{"format_id": "mp3-128", "ext": "mp3", "vcodec": "none", "acodec": "mp3", "abr": 128, "protocol": "https"}]
    options = build_options(fmts, has_ffmpeg=True, duration=60)
    assert {o.kind for o in options} == {"audio"}
    assert set(ids(options)) == {"audio-mp3", "audio-original"}


def test_no_formats():
    assert build_options([], has_ffmpeg=True) == []
    only_storyboards = [{"format_id": "sb", "ext": "mhtml", "vcodec": "none", "acodec": "none", "protocol": "mhtml"}]
    assert build_options(only_storyboards, has_ffmpeg=True) == []
    drm = [{"format_id": "x", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "has_drm": True}]
    assert build_options(drm, has_ffmpeg=True) == []


def test_unknown_sizes_are_not_invented():
    fmts = [{"format_id": "a", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 480, "protocol": "https"}]
    options = build_options(fmts, has_ffmpeg=False)
    assert options and all(o.size is None for o in options if o.kind == "video")


def test_exact_size_over_limit_is_dropped():
    options = build_options(SAMPLE_FORMATS, has_ffmpeg=True, max_filesize=50_000_000)
    got = ids(options)
    assert "video-1080-mp4" not in got
    assert "video-720-mp4" in got


def test_bucket_height():
    assert bucket_height(1080) == 1080
    assert bucket_height(1076) == 1080
    assert bucket_height(720) == 720
    assert bucket_height(544) == 720
    assert bucket_height(360) == 360


def test_normalise_skips_garbage():
    assert normalise_formats([None, "x", {"vcodec": "none", "acodec": "none"}]) == []
