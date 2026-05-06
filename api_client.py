"""Public-holiday API helpers."""

import datetime as dt
import re
from html import unescape
from typing import Iterable

import requests
import streamlit as st

HOLIDAYS_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
COUNTRIES_URL = "https://date.nager.at/api/v3/AvailableCountries"
CAFETERIA_MENU_URL = "https://monbistrot.pt/display/10"
CAFETERIA_WEEKLY_MENU_URL = (
    "https://monbistrot.pt/display-weekly/10?range=current-week"
)
CAFETERIA_WEEKLY_PAGE_URL_TEMPLATE = (
    "https://monbistrot.pt/menu/nova-sbe-semana-de-{start}-{end}"
)

_REQUEST_TIMEOUT = 5  # seconds
_CAFETERIA_TIMEOUT = 12


def _clean_menu_line(value) -> str:
    line = unescape(str(value or "")).strip()
    return re.sub(r"\s+", " ", line)


def _build_menu_sections(dishes: list[dict]) -> list[dict]:
    sections_by_key: dict[str, dict] = {}
    section_order: list[str] = []

    for dish in dishes:
        if not isinstance(dish, dict):
            continue

        language = _clean_menu_line(dish.get("language")).lower()
        if language and language != "en":
            continue

        title = _clean_menu_line(dish.get("category"))
        item = _clean_menu_line(dish.get("designation") or dish.get("type"))
        parenthetical = re.fullmatch(r"[^()]+\(([^()]+)\)\s*", item)
        if parenthetical:
            item = _clean_menu_line(parenthetical.group(1))
        if not title or not item:
            continue

        key = title.lower()
        if key not in sections_by_key:
            sections_by_key[key] = {"title": title, "items": []}
            section_order.append(key)

        items = sections_by_key[key]["items"]
        if item.lower() not in {existing.lower() for existing in items}:
            items.append(item)

    return [sections_by_key[key] for key in section_order
            if sections_by_key[key]["items"]]


def _format_menu_day_label(date_text: str) -> str:
    normalized = _clean_menu_line(date_text)
    try:
        parsed = dt.datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return normalized
    return f"{parsed:%A}, {parsed:%B} {parsed.day}"


def _format_week_label(start_date_text: str, end_date_text: str) -> str:
    try:
        start = dt.datetime.strptime(_clean_menu_line(start_date_text), "%Y-%m-%d")
        end = dt.datetime.strptime(_clean_menu_line(end_date_text), "%Y-%m-%d")
    except ValueError:
        return f"{start_date_text} - {end_date_text}"

    if start.month == end.month:
        month = start.strftime("%B")
        return f"{month} {start.day} - {month} {end.day}"
    return f"{start:%B} {start.day} - {end:%B} {end.day}"


def _current_week_bounds(
    reference_date: dt.date | None = None,
) -> tuple[dt.date, dt.date]:
    today = reference_date or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    friday = monday + dt.timedelta(days=4)
    return monday, friday


def _current_week_page_url() -> str:
    monday, friday = _current_week_bounds()
    return CAFETERIA_WEEKLY_PAGE_URL_TEMPLATE.format(
        start=monday.isoformat(),
        end=friday.isoformat(),
    )


def _weekly_page_day_label(day_token: str) -> str:
    mapping = {
        "segunda": "Monday",
        "terca": "Tuesday",
        "ter\u00e7a": "Tuesday",
        "quarta": "Wednesday",
        "quinta": "Thursday",
        "sexta": "Friday",
    }
    return mapping.get(_clean_menu_line(day_token).lower(), day_token.title())


def _build_weekly_sections_from_text(day_text: str) -> list[dict]:
    text = _clean_menu_line(day_text)
    if not text:
        return []

    label_patterns = [
        ("Soup", [r"Soup", r"Sopa"]),
        (
            "Meat or Fish",
            [r"Meat\s+Or\s+Fish\s*\|\s*EN", r"Meat\s+Or\s+Fish",
             r"Carne\s+ou\s+Peixe"],
        ),
        (
            "Green Vibes",
            [r"Green\s+Vibes\s*\|\s*EN", r"Green\s+Vibes", r"Vegetariano"],
        ),
        ("Nomad", [r"Nomad\s*\|\s*EN", r"Nomad"]),
    ]
    boundary = (
        r"(?:Soup|Sopa|Meat\s+Or\s+Fish(?:\s*\|\s*(?:EN|PT))?|"
        r"Carne\s+ou\s+Peixe|Green\s+Vibes(?:\s*\|\s*(?:EN|PT))?|"
        r"Vegetariano|Nomad(?:\s*\|\s*(?:EN|PT))?|"
        r"Segunda|Ter(?:c|\u00e7)a|Quarta|Quinta|Sexta|$)"
    )

    sections = []
    for title, patterns in label_patterns:
        item = ""
        for pattern in patterns:
            match = re.search(
                rf"{pattern}\s+(.*?)(?={boundary})",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                item = _clean_menu_line(match.group(1))
                if item:
                    break
        if item:
            sections.append({"title": title, "items": [item]})
    return sections


def _fetch_weekly_menu_from_page() -> dict:
    source_url = _current_week_page_url()
    response = requests.get(source_url, timeout=_CAFETERIA_TIMEOUT)
    response.raise_for_status()

    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        response.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)

    pattern = re.compile(r"\b(Segunda|Ter(?:c|\u00e7)a|Quarta|Quinta|Sexta)\b",
                         flags=re.IGNORECASE)
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError("Could not locate weekday blocks in weekly menu page")

    monday, _ = _current_week_bounds()
    day_index = {
        "segunda": 0,
        "terca": 1,
        "ter\u00e7a": 1,
        "quarta": 2,
        "quinta": 3,
        "sexta": 4,
    }
    days = []
    for idx, match in enumerate(matches):
        token_raw = _clean_menu_line(match.group(1))
        block_start = match.end()
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections = _build_weekly_sections_from_text(text[block_start:block_end])
        offset = day_index.get(token_raw.lower())
        if offset is None or not sections:
            continue
        day_date = monday + dt.timedelta(days=offset)
        days.append({
            "date": day_date.isoformat(),
            "date_label": f"{_weekly_page_day_label(token_raw)}, {day_date.isoformat()}",
            "sections": sections,
        })

    if not days:
        raise ValueError("Could not parse any weekly day sections")
    return {"source": source_url, "days": sorted(days, key=lambda d: d["date"])}


@st.cache_data(ttl=600, show_spinner=False)
def get_daily_cafeteria_menu() -> dict:
    """Fetch the Nova SBE daily cafeteria menu."""
    try:
        response = requests.get(CAFETERIA_MENU_URL, timeout=_CAFETERIA_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        menus = data.get("menus") or []
        if not menus:
            raise ValueError("Cafeteria source returned no menus")

        today = dt.date.today().isoformat()
        selected = next(
            (menu for menu in menus if menu.get("date") == today),
            menus[0],
        )
        sections = _build_menu_sections(selected.get("dishes") or [])
        if not sections:
            raise ValueError("Could not parse English daily menu")

        unit = data.get("unit") or {}
        return {
            "ok": True,
            "source": CAFETERIA_MENU_URL,
            "unit_name": _clean_menu_line(unit.get("name")) or "NOVA SBE",
            "date_label": _format_menu_day_label(selected.get("date") or today),
            "sections": sections,
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": CAFETERIA_MENU_URL,
            "error": str(exc),
            "sections": [],
        }


@st.cache_data(ttl=600, show_spinner=False)
def get_weekly_cafeteria_menu() -> dict:
    """Fetch the Nova SBE weekly cafeteria menu."""
    try:
        source_url = CAFETERIA_WEEKLY_MENU_URL
        data = {}
        menus = []
        try:
            response = requests.get(source_url, timeout=_CAFETERIA_TIMEOUT)
            response.raise_for_status()
            data = response.json() if response.text else {}
            menus = data.get("menus") or []
        except (requests.RequestException, ValueError):
            menus = []

        days = []
        if menus:
            for menu_day in menus:
                day_date = _clean_menu_line(menu_day.get("date"))
                sections = _build_menu_sections(menu_day.get("dishes") or [])
                if day_date and sections:
                    days.append({
                        "date": day_date,
                        "date_label": _format_menu_day_label(day_date),
                        "sections": sections,
                    })
        else:
            fallback = _fetch_weekly_menu_from_page()
            days = fallback["days"]
            source_url = fallback["source"]

        if not days:
            raise ValueError("Could not parse weekly menu")

        days.sort(key=lambda day: day.get("date", ""))
        unit = data.get("unit") or {}
        return {
            "ok": True,
            "source": source_url,
            "unit_name": _clean_menu_line(unit.get("name")) or "NOVA SBE",
            "week_label": _format_week_label(days[0]["date"], days[-1]["date"]),
            "days": days,
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": CAFETERIA_WEEKLY_MENU_URL,
            "error": str(exc),
            "days": [],
        }


@st.cache_data(ttl=86_400, show_spinner=False)
def fetch_public_holidays(country_code: str, year: int) -> list[dict]:
    """Fetch one country's holidays for a year."""
    try:
        r = requests.get(
            HOLIDAYS_URL.format(year=year, country=country_code.upper()),
            timeout=_REQUEST_TIMEOUT,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return []


def get_holiday_dates(country_code: str, years: Iterable[int]) -> set[dt.date]:
    """Return holiday dates only."""
    out: set[dt.date] = set()
    for y in years:
        for h in fetch_public_holidays(country_code, y):
            try:
                y2, m, d = map(int, h["date"].split("-"))
                out.add(dt.date(y2, m, d))
            except (KeyError, ValueError, TypeError):
                continue
    return out


def get_holidays_detailed(country_code: str, years: Iterable[int]) -> list[dict]:
    """Return holidays with date and names."""
    out = []
    for y in years:
        for h in fetch_public_holidays(country_code, y):
            try:
                y2, m, d = map(int, h["date"].split("-"))
                out.append({
                    "date": dt.date(y2, m, d),
                    "name": h.get("name", ""),
                    "local_name": h.get("localName", ""),
                })
            except (KeyError, ValueError, TypeError):
                continue
    out.sort(key=lambda x: x["date"])
    return out

_COUNTRY_FALLBACK = [
    ("DE", "Germany"), ("AT", "Austria"), ("CH", "Switzerland"),
    ("PT", "Portugal"), ("ES", "Spain"), ("FR", "France"),
    ("IT", "Italy"), ("NL", "Netherlands"), ("BE", "Belgium"),
    ("PL", "Poland"), ("GB", "United Kingdom"), ("IE", "Ireland"),
    ("US", "United States"), ("CA", "Canada"), ("BR", "Brazil"),
    ("MX", "Mexico"),
]


@st.cache_data(ttl=604_800, show_spinner=False)
def list_supported_countries() -> list[tuple[str, str]]:
    try:
        r = requests.get(COUNTRIES_URL, timeout=_REQUEST_TIMEOUT)
        if r.ok:
            data = r.json()
            return sorted(
                [(c["countryCode"], c["name"]) for c in data],
                key=lambda x: x[1],
            )
    except Exception:
        pass
    return _COUNTRY_FALLBACK
