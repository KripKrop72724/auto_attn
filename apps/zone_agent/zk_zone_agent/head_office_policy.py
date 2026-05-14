from __future__ import annotations

from urllib.parse import urlparse

from zk_zone_agent import settings as settings_module


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_head_office_url(value: str | None) -> str:
    settings = settings_module.settings
    configured = (value or settings.production_head_office_url).strip().rstrip("/")
    parsed = urlparse(configured)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Head office URL must include scheme and host.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Head office URL must be a bare base URL.")

    production = urlparse(settings.production_head_office_url.rstrip("/"))
    if settings.allow_dev_head_office_urls and parsed.hostname in LOCAL_HOSTS and parsed.scheme in {"http", "https"}:
        return configured

    if parsed.scheme != "https":
        raise ValueError("Head office URL must use HTTPS.")
    if parsed.netloc.lower() != production.netloc.lower():
        raise ValueError(f"Head office URL must be {settings.production_head_office_url}.")
    return configured
