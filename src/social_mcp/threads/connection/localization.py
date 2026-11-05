"""Connection-page catalogs and browser language negotiation."""

import json
from importlib.resources import files

DEFAULT_LANGUAGE = "en"
LANGUAGE_COOKIE = "social_mcp_language"
CATALOGS: dict[str, dict[str, str]] = {
    path.name.removesuffix(".json"): json.loads(path.read_text(encoding="utf-8"))
    for path in files(__package__).joinpath("locales").iterdir()
    if path.name.endswith(".json")
}
# Each catalog supplies its picker label; adding a catalog adds a switch option.
LANGUAGES = {code: catalog["language_name"] for code, catalog in CATALOGS.items()}


def choose_language(cookie: str | None, accept_language: str = "") -> str:
    """Prefer a supported manual choice, then the highest browser preference."""
    if cookie is not None and cookie in CATALOGS:
        return cookie
    preferences: list[tuple[float, str]] = []
    for entry in accept_language.split(","):
        tag, *parameters = entry.strip().split(";")
        code = tag.lower().split("-", 1)[0]
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if code in CATALOGS and 0 < quality <= 1:
            preferences.append((quality, code))
    return (
        max(preferences, key=lambda item: item[0])[1]
        if preferences
        else DEFAULT_LANGUAGE
    )


def translate(language: str, key: str) -> str:
    """Unknown languages fall back to English; missing keys fail explicitly."""
    return CATALOGS.get(language, CATALOGS[DEFAULT_LANGUAGE])[key]
