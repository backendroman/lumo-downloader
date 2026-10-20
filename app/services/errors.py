"""Error codes + mapping of yt-dlp messages to them"""

import re

# code -> (http status, message)
ERRORS: dict[str, tuple[int, str]] = {
    "invalid_url": (422, "Ссылка выглядит некорректно. Проверьте адрес и попробуйте ещё раз."),
    "unsupported_scheme": (422, "Поддерживаются только ссылки http:// и https://."),
    "forbidden_target": (422, "Ссылки на внутренние и служебные адреса не поддерживаются."),
    "dns_error": (422, "Не удалось найти сайт по этой ссылке."),
    "unsupported": (422, "Этот сайт или тип ссылки не поддерживается."),
    "playlist": (422, "Плейлисты и каналы не поддерживаются, вставьте ссылку на одно видео."),
    "live": (422, "Прямые трансляции не поддерживаются. Попробуйте после окончания эфира."),
    "no_formats": (422, "У этого медиа не найдено доступных для скачивания форматов."),
    "auth_required": (422, "Медиа доступно только после входа в аккаунт или является приватным."),
    "drm": (422, "Медиа защищено DRM, скачать его нельзя."),
    "source_blocked": (503, "Источник временно ограничил доступ для сервера (проверка на робота). Попробуйте позже."),
    "geo_blocked": (422, "Медиа недоступно из региона, где расположен сервер."),
    "unavailable": (422, "Медиа недоступно: возможно, оно удалено или ссылка устарела."),
    "rate_limited_source": (503, "Источник временно ограничил запросы. Попробуйте позже."),
    "too_long": (422, "Медиа слишком длинное для этого сервиса."),
    "too_large": (422, "Файл превышает допустимый размер."),
    "format_unavailable": (422, "Выбранный вариант больше недоступен. Выберите другое качество."),
    "ffmpeg_missing": (503, "На сервере не установлен FFmpeg, поэтому этот вариант недоступен."),
    "ffmpeg_error": (500, "Не удалось обработать файл (объединение или конвертация). Попробуйте другой формат."),
    "network_error": (502, "Не удалось связаться с источником. Проверьте ссылку или повторите позже."),
    "timeout": (504, "Источник отвечает слишком долго. Попробуйте ещё раз."),
    "disk_full": (507, "На сервере недостаточно места. Попробуйте позже."),
    "busy": (503, "Сервер сейчас загружен. Попробуйте через минуту."),
    "rate_limited": (429, "Слишком много запросов. Подождите немного и повторите."),
    "too_many_jobs": (429, "Дождитесь завершения текущих загрузок, прежде чем начинать новую."),
    "analysis_expired": (410, "Результат анализа устарел. Найдите медиа ещё раз."),
    "not_found": (404, "Задание не найдено. Возможно, оно устарело или сервер был перезапущен."),
    "not_ready": (409, "Файл ещё не готов."),
    "expired": (410, "Срок хранения файла истёк. Скачайте медиа заново."),
    "invalid_request": (422, "Некорректный запрос."),
    "payload_too_large": (413, "Слишком большой запрос."),
    "forbidden": (403, "Запрос отклонён."),
    "cancelled": (409, "Задание отменено."),
    "internal": (500, "Что-то пошло не так. Попробуйте ещё раз."),
}

# order matters, first match wins
_PATTERNS: list[tuple[str, str]] = [
    (r"blocked: non-public address", "forbidden_target"),
    (r"unsupported url|no suitable extractor|is not a valid url", "unsupported"),
    (r"\bdrm\b", "drm"),
    (r"not a bot|captcha|confirm you.re not|unusual traffic", "source_blocked"),
    (r"sign in|log ?in|login required|private video|members[- ]only|cookies|"
     r"confirm your age|age[- ]restricted|requires authentication|premium", "auth_required"),
    (r"not available in your country|geo.?restrict|not available from your location", "geo_blocked"),
    (r"live event will begin|is live|livestream|live stream", "live"),
    (r"larger than max-filesize|file is larger than|exceeds the size limit", "too_large"),
    # EFBIG from RLIMIT_FSIZE, must go before the ffmpeg pattern
    (r"file too large|errno 27\b", "too_large"),
    (r"no space left on device|disk full", "disk_full"),
    (r"requested format is not available|no video formats found|format not available", "format_unavailable"),
    (r"ffmpeg not found|ffprobe.*not found|ffmpeg is not installed", "ffmpeg_missing"),
    (r"postprocessing|ffmpeg|conversion failed|merg", "ffmpeg_error"),
    (r"http error 429|too many requests", "rate_limited_source"),
    (r"timed out|timeout", "timeout"),
    (r"video unavailable|has been removed|does not exist|not found|http error 404|"
     r"http error 410|unavailable|deleted|terminated", "unavailable"),
    (r"unable to download|getaddrinfo|name or service not known|connection|"
     r"network is unreachable|ssl|http error", "network_error"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def classify_ytdlp_error(message: str | None) -> str:
    text = _ANSI.sub("", message or "").lower()
    for pattern, code in _PATTERNS:
        if re.search(pattern, text):
            return code
    return "internal"


def error_status(code: str) -> int:
    return ERRORS.get(code, ERRORS["internal"])[0]


def error_message(code: str) -> str:
    return ERRORS.get(code, ERRORS["internal"])[1]


class AppError(Exception):
    def __init__(self, code: str, message: str | None = None, status: int | None = None):
        self.code = code if code in ERRORS else "internal"
        self.message = message or error_message(self.code)
        self.status = status or error_status(self.code)
        super().__init__(self.message)
