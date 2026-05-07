"""Streamlit interface for Nova Exam Planner."""

from __future__ import annotations

import datetime as dt
import base64
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

import database as db
import auth
import api_client as api
from api_client import get_motivational_quote
from ui import (
    apply_theme,
    fmt_hours,
    fmt_minutes,
    h,
    render_page_title,
    render_task_tile,
)
from scheduler import (
    DAY_NAMES,
    compute_analytics,
    generate_study_plan,
    rebalance_course_sessions,
)

APP_TITLE = "Nova Exam Planner"
APP_TAGLINE = "Study sessions planned around real exam dates."
DEFAULT_COUNTRY = "PT"
NOVA_LOGO_PATH = Path(__file__).parent / "assets" / "nova-logo-inverted.png"
NOVA_FAVICON_PATH = Path(__file__).parent / "assets" / "nova-favicon.png"

COURSE_COLORS = [
    "#111111", "#333333", "#555555", "#777777", "#999999",
    "#222222", "#444444", "#666666", "#888888", "#aaaaaa",
]


def estimate_hours(ects: float, difficulty: int) -> float:
    return round(ects * (1.5 + 0.5 * difficulty), 1)


def unique_course_name(base_name: str, existing_names: list[str]) -> str:
    """Return ``base_name`` or ``base_name (n)`` if the name already exists."""
    clean = base_name.strip()
    existing = {name.strip().lower() for name in existing_names}
    if clean.lower() not in existing:
        return clean

    idx = 2
    while f"{clean} ({idx})".lower() in existing:
        idx += 1
    return f"{clean} ({idx})"


def course_color(name: str, all_names: list[str]) -> str:
    sorted_names = sorted(set(all_names))
    idx = sorted_names.index(name) if name in sorted_names else 0
    return COURSE_COLORS[idx % len(COURSE_COLORS)]


def greeting() -> str:
    h = dt.datetime.now().hour
    if h < 5:
        return "Still awake"
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


def logo_data_uri() -> str:
    data = base64.b64encode(NOVA_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def go_to_page(page_name: str) -> None:
    st.session_state["pending_page_choice"] = page_name


def apply_pending_page_choice(page_names: list[str]) -> None:
    pending = st.session_state.pop("pending_page_choice", None)
    if pending in page_names:
        st.session_state["page_choice"] = pending


def sessions_as_df(user_id: int) -> pd.DataFrame:
    sessions = db.list_sessions(user_id)
    if not sessions:
        return pd.DataFrame(columns=[
            "id", "course_id", "course_name", "session_date",
            "planned_minutes", "completed_minutes",
        ])
    df = pd.DataFrame(sessions)
    df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
    return df


def courses_total_minutes(user_id: int) -> dict[int, float]:
    """Map course_id to required total study minutes."""
    return {c["id"]: c["estimated_hours"] * 60
            for c in db.list_courses(user_id)}


def render_capacity_warning(courses: list[dict], sessions_df: pd.DataFrame):
    """Warn when the generated plan cannot fit all requested study hours."""
    if not courses or sessions_df.empty:
        return

    target_min = int(sum(c["estimated_hours"] * 60 for c in courses))
    planned_min = int(sessions_df["planned_minutes"].sum())
    gap = target_min - planned_min
    if gap < 5:
        return

    st.warning(
        f"Your current limits only scheduled **{fmt_minutes(planned_min)}** "
        f"out of **{fmt_minutes(target_min)}** needed. "
        f"That leaves **{fmt_minutes(gap)} unscheduled**. "
        "Increase weekly study hours, max hours per day, available study "
        "days, or move the exam date further away."
    )


def missed_sessions_df(sessions_df: pd.DataFrame,
                       today: Optional[dt.date] = None) -> pd.DataFrame:
    """Return past sessions with unfinished planned minutes."""
    if today is None:
        today = dt.date.today()
    if sessions_df.empty:
        return sessions_df
    missed = sessions_df[
        (sessions_df["session_date"] < today)
        & (sessions_df["planned_minutes"] > sessions_df["completed_minutes"])
    ].copy()
    missed["missed_minutes"] = (
        missed["planned_minutes"] - missed["completed_minutes"]
    ).clip(lower=0).astype(int)
    return missed[missed["missed_minutes"] > 0]


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _floor_to_5(minutes: float) -> int:
    return max(0, int(minutes // 5) * 5)


def _reschedule_days(start: dt.date, end: dt.date,
                     preferred_days: list[str],
                     exclude_dates: set[dt.date]) -> list[dt.date]:
    out, cur = [], start
    preferred = set(preferred_days or DAY_NAMES)
    while cur < end:
        if DAY_NAMES[cur.weekday()] in preferred and cur not in exclude_dates:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def redistribute_missed_sessions(user: dict,
                                 missed: pd.DataFrame) -> tuple[int, int]:
    """Move missed minutes into future sessions while respecting constraints."""
    if missed.empty:
        return 0, 0

    today = dt.date.today()
    con = db.get_constraints(user["id"])
    courses = {c["id"]: c for c in db.list_courses(user["id"])}
    if not courses:
        return 0, int(missed["missed_minutes"].sum())

    max_daily = int(con["max_hours_per_day"]) * 60
    max_weekly = int(con["weekly_hours"]) * 60
    preferred_days = con["preferred_days"] or DAY_NAMES

    exclude: set[dt.date] = set()
    if con["skip_holidays"]:
        future_exams = [c["exam_date"] for c in courses.values()
                        if c["exam_date"] > today]
        if future_exams:
            end = max(future_exams)
            years = range(today.year, end.year + 1)
            exclude = api.get_holiday_dates(user["country_code"], years)

    sessions = db.list_sessions(user["id"])
    future_sessions = [s for s in sessions if s["session_date"] >= today]

    day_used = defaultdict(int)
    week_used = defaultdict(int)
    existing: dict[tuple[int, dt.date], dict] = {}
    for s in future_sessions:
        day = s["session_date"]
        planned = int(s["planned_minutes"])
        day_used[day] += planned
        week_used[_week_start(day)] += planned
        existing[(int(s["course_id"]), day)] = s

    missed_by_course = defaultdict(int)
    for _, row in missed.iterrows():
        missed_by_course[int(row["course_id"])] += int(row["missed_minutes"])

        # Keep completed time, drop only the missed part.
        completed = int(row["completed_minutes"])
        if completed > 0:
            db.update_session_planned(user["id"], int(row["id"]), completed)
        else:
            db.delete_session(user["id"], int(row["id"]))

    scheduled = 0
    unscheduled = 0
    ordered = sorted(
        missed_by_course.items(),
        key=lambda item: courses.get(item[0], {}).get("exam_date", today),
    )

    for course_id, minutes in ordered:
        course = courses.get(course_id)
        if not course or course["exam_date"] <= today:
            unscheduled += minutes
            continue

        remaining = minutes
        days = _reschedule_days(
            today, course["exam_date"], preferred_days, exclude)
        for day in days:
            if remaining < 5:
                break
            daily_room = max_daily - day_used[day]
            weekly_room = max_weekly - week_used[_week_start(day)]
            add = _floor_to_5(min(remaining, daily_room, weekly_room))
            if add <= 0:
                continue

            session = existing.get((course_id, day))
            if session:
                new_total = int(session["planned_minutes"]) + add
                db.update_session_planned(
                    user["id"], int(session["id"]), new_total)
                session["planned_minutes"] = new_total
            else:
                new_id = db.insert_session(user["id"], course_id, day, add)
                existing[(course_id, day)] = {
                    "id": new_id,
                    "course_id": course_id,
                    "session_date": day,
                    "planned_minutes": add,
                    "completed_minutes": 0,
                    "course_name": course["name"],
                }

            day_used[day] += add
            week_used[_week_start(day)] += add
            scheduled += add
            remaining -= add

        unscheduled += max(0, int(remaining))

    return scheduled, unscheduled


def render_adaptive_rescheduler(user: dict):
    """Show a dialog when past study time is unfinished."""
    sessions_df = sessions_as_df(user["id"])
    missed = missed_sessions_df(sessions_df)
    if missed.empty:
        st.session_state.pop("missed_rescheduler_dismissed", None)
        return

    signature = "|".join(
        f"{int(r.id)}:{int(r.planned_minutes)}:{int(r.completed_minutes)}"
        for r in missed.itertuples()
    )
    if st.session_state.get("missed_rescheduler_dismissed") == signature:
        return

    missed_total = int(missed["missed_minutes"].sum())
    by_course = (
        missed.groupby("course_name")["missed_minutes"]
        .sum().sort_values(ascending=False)
    )

    def body():
        st.write(
            f"You have **{fmt_minutes(missed_total)}** of unfinished study "
            "time from past sessions."
        )
        for course_name, minutes in by_course.items():
            st.caption(f"{course_name}: {fmt_minutes(int(minutes))}")

        c1, c2 = st.columns(2)
        if c1.button("Redistribute missed time", type="primary",
                     use_container_width=True):
            scheduled, unscheduled = redistribute_missed_sessions(user, missed)
            if scheduled:
                st.toast(f"Redistributed {fmt_minutes(scheduled)}.")
            if unscheduled:
                st.toast(
                    f"{fmt_minutes(unscheduled)} could not fit under your "
                    "current limits."
                )
            st.session_state["missed_rescheduler_dismissed"] = signature
            st.rerun()
        if c2.button("Remind me later", use_container_width=True):
            st.session_state["missed_rescheduler_dismissed"] = signature
            st.rerun()

    if hasattr(st, "dialog"):
        @st.dialog("Missed study time")
        def missed_dialog():
            body()
        missed_dialog()
    else:
        with st.container(border=True):
            st.subheader("Missed study time")
            body()


def page_auth():
    """Unauthenticated entry screen."""
    _, center, _ = st.columns([1, 2, 1])
    with center:
        render_page_title(
            APP_TITLE,
            APP_TAGLINE,
            "login",
            logo_src=logo_data_uri(),
        )

        tab_login, tab_register = st.tabs(["Log in", "Create account"])

        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                u = st.text_input("Username", key="login_u",
                                  placeholder="your_username")
                p = st.text_input("Password", type="password", key="login_p")
                submit = st.form_submit_button(
                    "Log in", type="primary", use_container_width=True)
            if submit:
                user = auth.authenticate(u.strip(), p)
                if user:
                    auth.login_session(user)
                    st.success(f"Welcome back, {user['display_name']}!")
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

        with tab_register:
            # Show persisted error above the form so it is always visible
            if st.session_state.get("_reg_error"):
                st.error(st.session_state.pop("_reg_error"))

            with st.form("register_form", clear_on_submit=False):
                c1, c2 = st.columns(2)
                with c1:
                    ru = st.text_input("Username *", key="reg_u",
                                       placeholder="pick_a_handle")
                with c2:
                    rd = st.text_input("Display name",
                                       placeholder="How should we call you?")
                re_mail = st.text_input(
                    "Email (optional)",
                    placeholder="you@example.com")

                countries = api.list_supported_countries()
                country_idx = next(
                    (i for i, (code, _) in enumerate(countries)
                     if code == DEFAULT_COUNTRY),
                    0)
                country_code = st.selectbox(
                    "Country (for public holidays)",
                    options=[c[0] for c in countries],
                    format_func=lambda c: next(
                        (f"{cc} - {name}" for cc, name in countries if cc == c),
                        c),
                    index=country_idx,
                )

                rp = st.text_input(
                    "Password *",
                    type="password",
                    help="Min 8 characters — needs at least one letter and one digit",
                )
                rp2 = st.text_input("Confirm password *", type="password")

                st.caption(
                    "Requirements: 3–20 character username (letters, digits, _) · "
                    "password ≥ 8 chars with a letter and a digit"
                )

                reg_submit = st.form_submit_button(
                    "Create account", type="primary",
                    use_container_width=True)

            if reg_submit:
                if rp != rp2:
                    st.session_state["_reg_error"] = "Passwords don't match."
                    st.rerun()
                else:
                    uid, err = auth.register_user(
                        ru.strip(), re_mail.strip(), rp,
                        display_name=rd.strip() or None,
                        country_code=country_code,
                    )
                    if err:
                        st.session_state["_reg_error"] = err
                        st.rerun()
                    else:
                        user = db.get_user_by_id(uid)
                        auth.login_session(user)
                        st.success("Account created. Welcome.")
                        st.rerun()


def page_dashboard(user: dict):
    courses = db.list_courses(user["id"])
    sessions_df = sessions_as_df(user["id"])

    render_page_title(
        f"{greeting()}, {user['display_name']}",
        f"Study snapshot for {dt.date.today():%A, %d %B %Y}.",
        "dashboard",
    )

    # Motivational quote
    quote = get_motivational_quote()
    if quote.get("ok") and quote.get("quote"):
        author_html = (
            f'<div class="q-author">— {h(quote["author"])}</div>'
            if quote.get("author") else ""
        )
        st.markdown(
            f'<div class="nova-quote">'
            f'<div class="q-text">"{h(quote["quote"])}"</div>'
            f'{author_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

    if not courses:
        st.info(
            "Start by opening Courses in the sidebar, adding your first "
            "course, and generating a plan."
        )
        _render_quickstart()
        return

    total_planned = (
        int(sessions_df["planned_minutes"].sum()) if not sessions_df.empty else 0
    )
    total_completed = (
        int(sessions_df["completed_minutes"].sum()) if not sessions_df.empty else 0
    )
    pct = (total_completed / total_planned * 100) if total_planned else 0
    next_exam = min(
        (c for c in courses if c["exam_date"] >= dt.date.today()),
        key=lambda c: c["exam_date"], default=None)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Courses", len(courses))
    k2.metric("Planned", fmt_minutes(total_planned))
    k3.metric("Completed", f"{pct:.0f}%",
              fmt_minutes(total_completed))
    if next_exam:
        days = (next_exam["exam_date"] - dt.date.today()).days
        k4.metric("Next exam",
                  f"{days} day{'s' if days != 1 else ''}",
                  next_exam["name"])
    else:
        k4.metric("Next exam", "-")

    render_capacity_warning(courses, sessions_df)

    st.divider()

    st.subheader("Today's Sessions")
    today = dt.date.today()
    today_tasks = sessions_df[sessions_df["session_date"] == today] \
        if not sessions_df.empty else pd.DataFrame()
    if today_tasks.empty:
        st.caption("No sessions today.")
    else:
        all_names = [c["name"] for c in courses]
        for _, row in today_tasks.iterrows():
            clr = course_color(row["course_name"], all_names)
            render_task_tile(
                row["course_name"],
                int(row["planned_minutes"]),
                int(row["completed_minutes"]),
                clr,
            )

    st.divider()

    st.subheader("Upcoming Exams")
    upcoming = [c for c in courses
                if 0 <= (c["exam_date"] - today).days <= 21]
    upcoming.sort(key=lambda c: c["exam_date"])
    if not upcoming:
        st.caption("No exams in the next three weeks.")
    else:
        for c in upcoming:
            d = (c["exam_date"] - today).days
            chip_class = "chip-urgent" if d <= 3 \
                else "chip-warn" if d <= 10 else "chip-ok"
            label = "today!" if d == 0 \
                else "tomorrow" if d == 1 \
                else f"in {d} days"
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;margin-bottom:6px;">
                    <span class="nova-chip {chip_class}">{h(label)}</span>
                    <strong>{h(c['name'])}</strong>
                    <span style="margin-left:auto;color:var(--muted);">
                        {c['exam_date']:%a, %d %b}
                    </span>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_quickstart():
    """Three-step intro for brand-new users."""
    st.markdown("### First setup")
    cards = [
        (
            "1. Add courses",
            "Name, exam date, ECTS and difficulty. This opens the course form.",
        ),
        (
            "2. Set constraints",
            "Choose weekly hours, study days, daily caps, and holidays.",
        ),
        (
            "3. Generate plan",
            "Build the balanced schedule after courses and limits are ready.",
        ),
    ]
    with st.container(key="quickstart_actions"):
        cols = st.columns(3)
        for idx, (title, body) in enumerate(cards):
            with cols[idx]:
                st.button(
                    f"**{title}**\n\n{body}",
                    key=f"quickstart_action_{idx}",
                    use_container_width=True,
                    on_click=go_to_page,
                    args=("Courses",),
                )


def _cafeteria_lunch_summary(menu: dict) -> str:
    sections = menu.get("sections") or []
    priority = ("Meat or Fish", "Green Vibes", "Nomad", "Soup")
    for preferred in priority:
        section = next(
            (s for s in sections
             if preferred.lower() in str(s.get("title", "")).lower()),
            None,
        )
        if section and section.get("items"):
            return f"{section['title']}: {section['items'][0]}"

    if sections and sections[0].get("items"):
        return f"{sections[0]['title']}: {sections[0]['items'][0]}"
    return "check today's cafeteria options before scheduling long blocks."


def _weekly_cafeteria_html(days: list[dict]) -> str:
    cards = []
    for day in days:
        rows = []
        for section in day.get("sections", []):
            items = section.get("items") or []
            if not items:
                continue
            rows.append(
                '<div class="nova-menu-row">'
                f'<span>{h(section.get("title") or "Menu")}</span>'
                f'<p>{h(" / ".join(items))}</p>'
                '</div>'
            )
        if rows:
            cards.append(
                '<section class="nova-menu-day">'
                f'<h4>{h(day.get("date_label") or "Menu")}</h4>'
                f'{"".join(rows)}'
                '</section>'
            )
    return f'<div class="nova-weekly-menu">{"".join(cards)}</div>'


def page_cafeteria(user: dict):
    render_page_title(
        "Cafeteria",
        "Nova SBE lunch menus from MON BISTRO.",
        "lunch",
    )

    daily = api.get_daily_cafeteria_menu()
    weekly = api.get_weekly_cafeteria_menu()
    if not daily.get("ok") and not weekly.get("ok"):
        st.info("Cafeteria menu is unavailable right now.")
        st.caption("Source: monbistrot.pt")
        return

    if daily.get("ok"):
        st.markdown(
            f'<div class="nova-lunch-line">'
            f'<strong>Today:</strong> {h(_cafeteria_lunch_summary(daily))}'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"{daily.get('unit_name', 'NOVA SBE')} - "
            f"{daily.get('date_label', '')}"
        )
    else:
        st.info("Today's cafeteria menu is unavailable right now.")

    st.divider()
    st.subheader("Weekly Menu")
    if not weekly.get("ok"):
        st.info("Weekly cafeteria menu is unavailable right now.")
        return

    st.caption(f"{weekly.get('unit_name', 'NOVA SBE')} - "
               f"{weekly.get('week_label', '')}")
    st.markdown(_weekly_cafeteria_html(weekly.get("days", [])),
                unsafe_allow_html=True)


def page_courses(user: dict):
    render_page_title(
        "Courses & Constraints",
        "Add exams, set your available time, then build the study plan.",
        "setup",
    )

    st.subheader("Study Constraints")
    con = db.get_constraints(user["id"])

    c1, c2, c3 = st.columns(3)
    with c1:
        weekly_hours = st.number_input(
            "Weekly study hours", 1, 80, int(con["weekly_hours"]))
    with c2:
        max_daily = st.number_input(
            "Max hours / day", 1, 16, int(con["max_hours_per_day"]))
    with c3:
        start_date = st.date_input(
            "Plan start date", con["start_date"])

    _pd_all = list(DAY_NAMES)
    _pd_ver = st.session_state.get("pref_days_ver", 0)
    if "pref_days_value" not in st.session_state:
        st.session_state["pref_days_value"] = con["preferred_days"] or DAY_NAMES

    _days_all_selected = set(st.session_state.get("pref_days_value") or []) == set(_pd_all)
    _toggle_opt = "✕" if _days_all_selected else "✓"
    _pills_result = st.pills(
        "Preferred study days",
        _pd_all + [_toggle_opt],
        default=st.session_state["pref_days_value"],
        selection_mode="multi",
        key=f"pref_days_pills_{_pd_ver}",
    )
    if _toggle_opt in (_pills_result or []):
        st.session_state["pref_days_value"] = [] if _days_all_selected else _pd_all
        st.session_state["pref_days_ver"] = _pd_ver + 1
        st.rerun()
    preferred_days = [d for d in (_pills_result or []) if d in _pd_all]
    st.session_state["pref_days_value"] = preferred_days
    if not preferred_days:
        st.warning("Select at least one study day.")
        preferred_days = list(DAY_NAMES)

    c4, c5 = st.columns([2, 1])
    with c4:
        skip_holidays = st.checkbox(
            f"Skip public holidays ({user['country_code']})",
            value=bool(con["skip_holidays"]),
            help="Fetches holidays from date.nager.at and excludes them "
                 "from the schedule.",
        )
    with c5:
        if st.button("Save settings", use_container_width=True):
            db.save_constraints(
                user["id"],
                weekly_hours=weekly_hours,
                preferred_days=preferred_days,
                max_hours_per_day=max_daily,
                start_date=start_date,
                skip_holidays=skip_holidays,
            )
            st.toast("Settings saved.")

    if skip_holidays:
        with st.expander("Show upcoming public holidays"):
            years = sorted({start_date.year, start_date.year + 1})
            holidays = api.get_holidays_detailed(
                user["country_code"], years)
            future = [hol for hol in holidays if hol["date"] >= dt.date.today()][:12]
            if not future:
                st.caption("No upcoming holidays found.")
            else:
                for hol in future:
                    st.markdown(
                        f"- **{hol['date']:%d %b %Y}** - "
                        f"{hol['local_name']} ({hol['name']})")

    st.divider()

    editing_id = st.session_state.get("editing_course_id")
    editing = db.get_course(user["id"], editing_id) if editing_id else None
    st.subheader("Edit course" if editing else "Add course")
    existing_courses = db.list_courses(user["id"])

    defaults = editing or {
        "name": "",
        "exam_date": dt.date.today() + dt.timedelta(days=30),
        "ects": 6.0,
        "difficulty": 3,
        "estimated_hours": 0.0,
    }

    # ECTS and difficulty live outside the form so changes update the
    # estimated-hours preview instantly on every slider/input interaction.
    ects_key = f"ects_live_{editing_id or 'new'}"
    diff_key = f"diff_live_{editing_id or 'new'}"

    lc1, lc2 = st.columns(2)
    with lc1:
        ects = st.number_input(
            "ECTS", 0.5, 30.0, float(defaults["ects"]),
            step=0.5, format="%.1f", key=ects_key)
    with lc2:
        difficulty = st.slider(
            "Difficulty (1-5)", 1, 5, int(defaults["difficulty"]),
            key=diff_key)

    auto_est = estimate_hours(ects, difficulty)
    st.markdown(
        f'<div class="nova-est-hours">'
        f'<span class="est-label">Estimated study hours</span>'
        f'<span class="est-value">{fmt_hours(auto_est)}</span>'
        f'<span class="est-formula">'
        f'= {ects:g} ECTS × (1.5 + 0.5 × {difficulty}) difficulty'
        f'</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    form_ver = st.session_state.get("course_form_version", 0)
    with st.form(f"course_form_{form_ver}", clear_on_submit=True):
        fc1, fc2 = st.columns(2)
        with fc1:
            name = st.text_input("Course name", defaults["name"])
        with fc2:
            min_exam = dt.date.today() + dt.timedelta(days=1)
            default_exam = max(defaults["exam_date"], min_exam)
            exam_date = st.date_input(
                "Exam date", default_exam,
                min_value=min_exam,
                help="Must be at least tomorrow.")

        estimated = st.number_input(
            "Override study hours (0 = use auto estimate above)",
            min_value=0.0,
            value=float(defaults["estimated_hours"])
                  if defaults["estimated_hours"] > 0 else 0.0,
            step=0.5,
            format="%.1f",
        )

        other_names = [
            c["name"] for c in existing_courses
            if not editing_id or c["id"] != editing_id
        ]
        duplicate_name = (
            bool(name.strip())
            and name.strip().lower() in {n.lower() for n in other_names}
        )
        separated_name = unique_course_name(name, other_names) \
            if name.strip() else ""
        separate_duplicate = False
        if duplicate_name:
            st.warning(
                f"A course named **{name.strip()}** already exists. "
                f"To keep both, save this one as **{separated_name}**."
            )
            separate_duplicate = st.checkbox(
                f"Save anyway as {separated_name}",
                help="Nova keeps course names unique so schedules and exports "
                     "can clearly tell them apart.",
            )

        btn_label = "Update course" if editing else "Add course"
        submitted = st.form_submit_button(btn_label, type="primary")

        if submitted and name.strip():
            if exam_date <= dt.date.today():
                st.error("Exam date must be in the future (at least tomorrow).")
                return
            final_name = separated_name if duplicate_name and separate_duplicate \
                else name.strip()
            if duplicate_name and not separate_duplicate:
                st.error(
                    "That course name already exists. Tick the checkbox to "
                    "save a separated copy, or choose a different name."
                )
                return
            final_hours = estimated if estimated > 0 else auto_est
            try:
                db.upsert_course(
                    user["id"], final_name, exam_date,
                    ects, difficulty, final_hours,
                    course_id=editing_id,
                )
                st.session_state["editing_course_id"] = None
                st.session_state["course_form_version"] = form_ver + 1
                # Reset live-update keys so the form shows fresh defaults
                for k in (ects_key, diff_key):
                    st.session_state.pop(k, None)
                st.toast(f"{'Updated' if editing else 'Added'} {final_name}.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save course: {e}")

    if editing and st.button("Cancel editing"):
        st.session_state["editing_course_id"] = None
        st.session_state["course_form_version"] = form_ver + 1
        for k in (ects_key, diff_key):
            st.session_state.pop(k, None)
        st.rerun()

    st.divider()

    st.subheader("Your courses")
    courses = db.list_courses(user["id"])
    if not courses:
        st.info("No courses yet. Add one above to get started.")
        return

    for c in courses:
        days_until = (c["exam_date"] - dt.date.today()).days
        if days_until < 0:
            status_txt = "Past"
            status_color = "var(--muted)"
        elif days_until <= 7:
            status_txt = f"⚠ {days_until}d left"
            status_color = "var(--warn)"
        elif days_until <= 21:
            status_txt = f"{days_until}d left"
            status_color = "var(--ink)"
        else:
            status_txt = f"{days_until}d left"
            status_color = "var(--muted)"

        with st.container(border=True):
            cols = st.columns([0.28, 0.09, 0.09, 0.15, 0.13, 0.13, 0.07, 0.06])
            cols[0].markdown(f"**{h(c['name'])}**", unsafe_allow_html=False)
            cols[1].caption(f"ECTS {c['ects']:g}")
            cols[2].caption(f"Diff {c['difficulty']}/5")
            cols[3].caption(f"Est. {fmt_hours(c['estimated_hours'])}")
            cols[4].caption(f"Exam: {c['exam_date']:%d %b %Y}")
            cols[5].markdown(
                f'<span style="font-size:0.82rem;color:{status_color};">'
                f'{h(status_txt)}</span>',
                unsafe_allow_html=True)
            with cols[6]:
                st.markdown('<div class="nova-btn-edit">', unsafe_allow_html=True)
                if st.button("Edit", key=f"edit_{c['id']}", use_container_width=True):
                    st.session_state["editing_course_id"] = c["id"]
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with cols[7]:
                st.markdown('<div class="nova-btn-delete">', unsafe_allow_html=True)
                if st.button("✕", key=f"del_{c['id']}", help=f"Delete {c['name']}",
                             use_container_width=True):
                    db.delete_course(user["id"], c["id"])
                    st.toast(f"Deleted {c['name']}.")
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

    st.divider()

    st.subheader("Generate study plan")
    if st.button("Generate balanced plan", type="primary",
                 use_container_width=True):
        with st.spinner("Building your schedule..."):
            db.save_constraints(
                user["id"],
                weekly_hours=weekly_hours,
                preferred_days=preferred_days,
                max_hours_per_day=max_daily,
                start_date=start_date,
                skip_holidays=skip_holidays,
            )

            exclude = set()
            if skip_holidays:
                end = max((c["exam_date"] for c in courses),
                          default=start_date)
                years = list(range(start_date.year, end.year + 1))
                exclude = api.get_holiday_dates(
                    user["country_code"], years)

            sessions = generate_study_plan(
                courses,
                preferred_days=preferred_days,
                max_hours_per_day=max_daily,
                start_date=start_date,
                weekly_hours=weekly_hours,
                exclude_dates=exclude,
            )

        if not sessions:
            st.error(
                "Could not generate a plan.  Check that your exam dates "
                "are in the future and at least one study day is selected.")
            return

        db.replace_sessions(user["id"], sessions)
        total_min = sum(s["planned_minutes"] for s in sessions)
        target_min = int(sum(c["estimated_hours"] * 60 for c in courses))
        st.success(
            f"Plan generated: {len(sessions)} sessions across "
            f"{len({s['session_date'] for s in sessions})} days "
            f"({fmt_minutes(total_min)} total). "
            "Open Study Plan to review it.")
        if total_min + 5 < target_min:
            st.warning(
                f"Your daily/weekly limits leave "
                f"{fmt_minutes(target_min - total_min)} unscheduled. "
                "Increase your weekly hours, daily cap, or available study "
                "days if you want to fit the full target."
            )


def page_study_plan(user: dict):
    render_page_title(
        "Study Plan",
        "Weekly calendar, today's work, and completion tracking.",
        "plan",
    )

    sessions_df = sessions_as_df(user["id"])
    if sessions_df.empty:
        st.info("Generate a plan on the Courses page first.")
        return

    courses = db.list_courses(user["id"])
    all_names = [c["name"] for c in courses]
    today = dt.date.today()

    render_capacity_warning(courses, sessions_df)

    today_tasks = sessions_df[sessions_df["session_date"] == today]
    if not today_tasks.empty:
        st.subheader("Today")
        for _, row in today_tasks.iterrows():
            clr = course_color(row["course_name"], all_names)
            render_task_tile(
                row["course_name"],
                int(row["planned_minutes"]),
                int(row["completed_minutes"]),
                clr,
            )
        st.divider()

    st.subheader("Weekly Calendar")

    min_d = sessions_df["session_date"].min()
    max_d = sessions_df["session_date"].max()

    weeks = []
    ws = min_d - dt.timedelta(days=min_d.weekday())
    while ws <= max_d:
        weeks.append(ws)
        ws += dt.timedelta(days=7)

    week_labels = [
        f"{w:%d %b} - {(w + dt.timedelta(days=6)):%d %b %Y}" for w in weeks]
    cur_mon = today - dt.timedelta(days=today.weekday())
    default_idx = max((i for i, w in enumerate(weeks) if w <= cur_mon),
                      default=0)

    sel_label = st.selectbox(
        "Select week", week_labels,
        index=min(default_idx, len(week_labels) - 1))
    sel_week = weeks[week_labels.index(sel_label)]

    cols = st.columns(7)
    for offset in range(7):
        day = sel_week + dt.timedelta(days=offset)
        day_data = sessions_df[sessions_df["session_date"] == day]
        is_today = (day == today)

        with cols[offset]:
            day_class = "calendar-day today" if is_today else "calendar-day"
            st.markdown(
                f"""
                <div class="{day_class}">
                    <small>{DAY_NAMES[day.weekday()][:3]}</small><br>
                    <strong style="font-size:1.1em;">{day:%d}</strong>
                </div>
                """, unsafe_allow_html=True)

            if day_data.empty:
                st.caption("-")
            else:
                for _, row in day_data.iterrows():
                    clr = course_color(row["course_name"], all_names)
                    st.markdown(
                        f"""
                        <div class="mini-session" style="border-left-color:{h(clr)};">
                            <div>{h(row['course_name'])}</div>
                            <strong>{fmt_minutes(row['planned_minutes'])}</strong>
                        </div>
                        """, unsafe_allow_html=True)
                st.caption(
                    f"Total: {fmt_minutes(day_data['planned_minutes'].sum())}")

            for c in courses:
                if c["exam_date"] == day:
                    st.markdown(
                        f"""
                        <div class="exam-tag">
                            Exam: {h(c['name'])}
                        </div>
                        """, unsafe_allow_html=True)

    st.divider()

    st.subheader("Progress Tracking")
    past = sessions_df[sessions_df["session_date"] <= today] \
        .sort_values("session_date", ascending=False)
    if past.empty:
        st.info("No past sessions yet.")
    else:
        cutoff = today - dt.timedelta(days=14)
        recent = past[past["session_date"] >= cutoff]
        for date_val, grp in recent.groupby("session_date", sort=False):
            label = f"{date_val:%A, %d %b}"
            if date_val == today:
                label += " - today"
            with st.expander(label, expanded=(date_val == today)):
                for _, row in grp.iterrows():
                    was_done = (row["completed_minutes"] >=
                                row["planned_minutes"] > 0)
                    k = f"chk_{row['id']}"
                    new_val = st.checkbox(
                        f"{row['course_name']} - "
                        f"{fmt_minutes(row['planned_minutes'])}",
                        value=was_done, key=k)
                    if new_val and not was_done:
                        db.update_session_completed(
                            user["id"], row["id"],
                            int(row["planned_minutes"]))
                        st.rerun()
                    elif not new_val and was_done:
                        db.update_session_completed(
                            user["id"], row["id"], 0)
                        st.rerun()

    st.divider()

    st.subheader("Course Progress")
    for c in courses:
        cp = sessions_df[sessions_df["course_id"] == c["id"]]
        total_p = int(cp["planned_minutes"].sum()) if not cp.empty else 0
        total_c = int(cp["completed_minutes"].sum()) if not cp.empty else 0
        pct = (total_c / total_p) if total_p else 0
        st.markdown(
            f"**{c['name']}** - {fmt_minutes(total_c)} / {fmt_minutes(total_p)}")
        st.progress(min(pct, 1.0))


def page_customize(user: dict):
    render_page_title(
        "Customize Plan",
        "Adjust session durations or rebalance a course to hit its target.",
        "edit",
    )

    sessions_df = sessions_as_df(user["id"])
    if sessions_df.empty:
        st.info("Generate a plan on the Courses page first.")
        return

    courses = db.list_courses(user["id"])
    course_names = sorted(sessions_df["course_name"].unique())
    totals = courses_total_minutes(user["id"])

    # ── Plan health summary ───────────────────────────────────────────────
    st.subheader("Plan health")
    per_course_sum = (
        sessions_df.groupby(["course_id", "course_name"])["planned_minutes"]
        .sum().reset_index()
    )
    all_ok = True
    for _, r in per_course_sum.iterrows():
        target_min = int(totals.get(int(r["course_id"]), 0))
        planned_min = int(r["planned_minutes"])
        diff = planned_min - target_min
        if abs(diff) < 10:
            icon, color = "✓", "var(--success)"
            diff_str = "on target"
        elif diff > 0:
            icon, color = "↑", "var(--muted)"
            diff_str = f"+{fmt_minutes(diff)} over"
        else:
            icon, color = "!", "var(--warn)"
            diff_str = f"{fmt_minutes(abs(diff))} short"
            all_ok = False
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:0.5rem;'
            f'margin-bottom:0.4rem;">'
            f'<span style="font-weight:900;color:{color};font-size:1.1rem;">'
            f'{icon}</span>'
            f'<strong>{h(r["course_name"])}</strong>'
            f'<span style="margin-left:auto;color:var(--muted);font-size:0.85rem;">'
            f'Planned {fmt_minutes(planned_min)} / Target {fmt_minutes(target_min)}'
            f' · <em>{diff_str}</em>'
            f'</span></div>',
            unsafe_allow_html=True,
        )
    if all_ok:
        st.success("All courses are on target.", icon="✅")

    st.divider()

    # ── Two tabs: Edit sessions | Rebalance ───────────────────────────────
    tab_edit, tab_rebalance = st.tabs(["✏️  Edit Sessions", "⚖️  Rebalance Course"])

    with tab_edit:
        st.caption(
            "Filter by course or week, then change **Planned Minutes** for "
            "any session and hit **Save changes**."
        )

        cf1, cf2 = st.columns(2)
        with cf1:
            _cc_ver = st.session_state.get("cust_course_ver", 0)
            if "cust_course_value" not in st.session_state:
                st.session_state["cust_course_value"] = list(course_names)

            _cc_all_sel = set(st.session_state.get("cust_course_value") or []) == set(course_names)
            _cc_toggle = "✕" if _cc_all_sel else "✓"
            _cc_result = st.pills(
                "Filter by course", list(course_names) + [_cc_toggle],
                default=st.session_state["cust_course_value"],
                selection_mode="multi", key=f"cust_course_filter_{_cc_ver}")
            if _cc_toggle in (_cc_result or []):
                st.session_state["cust_course_value"] = [] if _cc_all_sel else list(course_names)
                st.session_state["cust_course_ver"] = _cc_ver + 1
                st.rerun()
            sel_courses = [c for c in (_cc_result or []) if c in course_names]
            st.session_state["cust_course_value"] = sel_courses
        filtered = sessions_df[sessions_df["course_name"].isin(sel_courses or [])]

        with cf2:
            if not filtered.empty:
                weeks = sorted(
                    pd.to_datetime(filtered["session_date"])
                      .dt.isocalendar().week.unique())
                if len(weeks) > 1:
                    wk = st.select_slider(
                        "Filter by calendar week",
                        options=weeks, value=(weeks[0], weeks[-1]),
                        key="cust_week_filter")
                    mask_week = pd.to_datetime(
                        filtered["session_date"]).dt.isocalendar().week.between(*wk)
                    filtered = filtered[mask_week]

        edit_df = filtered[
            ["id", "session_date", "course_name", "planned_minutes",
             "completed_minutes"]
        ].rename(columns={"session_date": "date"}).copy()
        edit_df["planned_minutes"] = edit_df["planned_minutes"].astype(int)
        edit_df["completed_minutes"] = edit_df["completed_minutes"].astype(int)

        edited = st.data_editor(
            edit_df,
            column_config={
                "id": st.column_config.NumberColumn(
                    "ID", disabled=True, width="small"),
                "date": st.column_config.DateColumn("Date", disabled=True),
                "course_name": st.column_config.TextColumn(
                    "Course", disabled=True),
                "planned_minutes": st.column_config.NumberColumn(
                    "Planned min", min_value=0, max_value=600, step=5,
                    help="Edit this column — multiples of 5 work best."),
                "completed_minutes": st.column_config.NumberColumn(
                    "Done min", disabled=True),
            },
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="plan_editor",
        )

        if st.button("Save changes", type="primary", use_container_width=True):
            changed = 0
            for _, row in edited.iterrows():
                orig = sessions_df.loc[
                    sessions_df["id"] == row["id"], "planned_minutes"]
                if not orig.empty and int(orig.iloc[0]) != int(row["planned_minutes"]):
                    db.update_session_planned(
                        user["id"], int(row["id"]), int(row["planned_minutes"]))
                    changed += 1
            st.toast(
                f"Saved {changed} change{'s' if changed != 1 else ''}."
                if changed else "No changes to save.")
            st.rerun()

    with tab_rebalance:
        st.markdown(
            "**Rebalancing** redistributes a course's remaining sessions so "
            "the total scheduled time matches its study-hours target. "
            "Completed sessions are never touched."
        )
        rebalance_course = st.selectbox(
            "Course to rebalance", course_names, key="cust_rebalance_sel",
            label_visibility="visible")

        course_obj = next(
            (c for c in courses if c["name"] == rebalance_course), None)
        if course_obj:
            cp = sessions_df[sessions_df["course_id"] == course_obj["id"]]
            target_min = int(course_obj["estimated_hours"] * 60)
            planned_min = int(cp["planned_minutes"].sum()) if not cp.empty else 0
            diff = planned_min - target_min
            st.caption(
                f"Target: {fmt_minutes(target_min)} · "
                f"Currently planned: {fmt_minutes(planned_min)} · "
                f"Gap: {'+' if diff >= 0 else ''}{fmt_minutes(abs(diff))}"
            )

        if st.button("Rebalance this course", type="primary",
                     use_container_width=True, key="cust_rebalance_btn"):
            if course_obj:
                sdf = sessions_as_df(user["id"])
                sdf = rebalance_course_sessions(
                    sdf.copy(), course_obj["id"],
                    course_obj["estimated_hours"] * 60)
                for _, row in sdf.iterrows():
                    db.update_session_planned(
                        user["id"], int(row["id"]),
                        int(row["planned_minutes"]))
                st.toast(f"Rebalanced {rebalance_course}.")
                st.rerun()


_ANALYTICS_PALETTE = [
    "#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626",
    "#0891b2", "#65a30d", "#c026d3", "#ea580c", "#0284c7",
]


def _analytics_cmap(course_names: list[str]) -> dict[str, str]:
    return {name: _ANALYTICS_PALETTE[i % len(_ANALYTICS_PALETTE)]
            for i, name in enumerate(sorted(set(course_names)))}


def page_analytics(user: dict):
    import plotly.express as px
    import plotly.graph_objects as go

    render_page_title(
        "Analytics",
        "Check workload, course balance, and completion progress.",
        "stats",
    )

    sessions_df = sessions_as_df(user["id"])
    if sessions_df.empty:
        st.info("Generate a plan first to see analytics.")
        return

    a = compute_analytics(sessions_df)
    con = db.get_constraints(user["id"])

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total planned", fmt_minutes(a["total_planned"]))
    k2.metric("Study days", a["total_days"])
    k3.metric("Completed", fmt_minutes(a["total_completed"]))
    k4.metric("Remaining", fmt_minutes(a["total_remaining"]))

    pct_overall = a.get("pct", 0)
    st.progress(min(pct_overall / 100, 1.0))
    st.caption(f"Overall completion: {pct_overall:.1f}%")

    st.divider()

    courses = db.list_courses(user["id"])
    all_names = [c["name"] for c in courses]
    cmap = _analytics_cmap(all_names)

    chart_layout = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, sans-serif", color="#111111"),
        margin=dict(l=0, r=20, t=14, b=0),
    )

    st.subheader("Study time per course")
    pc = a["per_course"].sort_values("planned", ascending=True).copy()
    pc["label"] = pc["planned"].apply(fmt_minutes)
    fig1 = px.bar(
        pc, x="planned", y="course_name", orientation="h",
        color="course_name", color_discrete_map=cmap, text="label",
        labels={"planned": "Planned Minutes", "course_name": ""})
    fig1.update_traces(textposition="outside")
    fig1.update_layout(
        showlegend=False,
        height=max(250, len(pc) * 64),
        xaxis=dict(showgrid=True, gridcolor="#eeeeee"),
        yaxis=dict(showgrid=False),
        **chart_layout)
    st.plotly_chart(fig1, use_container_width=True)

    st.divider()

    st.subheader("Weekly workload")
    weekly = a["weekly"].copy()
    weekly["hours"] = weekly["planned_minutes"] / 60
    fig2 = px.bar(
        weekly, x="week_label", y="hours",
        color="course_name", color_discrete_map=cmap,
        labels={"week_label": "Week of", "hours": "Hours",
                "course_name": "Course"},
        category_orders={"week_label": a["week_order"]})
    fig2.update_layout(
        barmode="stack", height=380,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#eeeeee"),
        legend=dict(orientation="h", y=-0.25),
        **chart_layout)
    st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    st.subheader("Daily load")
    daily = a["daily"].copy()
    daily["hours"] = daily["total_minutes"] / 60
    fig3 = px.area(
        daily, x="date", y="hours",
        color_discrete_sequence=["#2563eb"],
        labels={"date": "Date", "hours": "Hours"})
    fig3.update_traces(fillcolor="rgba(37,99,235,0.12)", line_color="#2563eb")
    fig3.add_hline(
        y=int(con["max_hours_per_day"]),
        line_dash="dash", line_color="#dc2626", line_width=1.5,
        annotation_text="Daily max", annotation_font_color="#dc2626")
    fig3.update_layout(
        height=300,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="#eeeeee"),
        **chart_layout)
    st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    st.subheader("Completed vs remaining")
    pc2 = a["per_course"].copy()
    fig4 = go.Figure()
    fig4.add_trace(go.Bar(
        name="Completed", x=pc2["course_name"],
        y=pc2["completed"] / 60,
        marker=dict(color=[cmap.get(n, "#2563eb") for n in pc2["course_name"]])))
    fig4.add_trace(go.Bar(
        name="Remaining", x=pc2["course_name"],
        y=pc2["remaining"] / 60,
        marker=dict(
            color=[cmap.get(n, "#2563eb") for n in pc2["course_name"]],
            opacity=0.25)))
    fig4.update_layout(
        barmode="stack", height=380,
        yaxis=dict(title="Hours", showgrid=True, gridcolor="#eeeeee"),
        xaxis=dict(showgrid=False),
        legend=dict(orientation="h", y=-0.15),
        **chart_layout)
    st.plotly_chart(fig4, use_container_width=True)

    st.divider()

    st.subheader("Completion progress by course")
    for _, r in a["per_course"].iterrows():
        pct = r["pct"] / 100
        color = cmap.get(r["course_name"], "#2563eb")
        st.markdown(
            f'<div style="display:flex;align-items:center;'
            f'justify-content:space-between;margin-bottom:2px;">'
            f'<strong>{h(r["course_name"])}</strong>'
            f'<span style="font-size:0.82rem;color:var(--muted);">'
            f'{fmt_minutes(r["completed"])} / {fmt_minutes(r["planned"])} '
            f'({r["pct"]:.0f}%)</span>'
            f'</div>',
            unsafe_allow_html=True)
        st.progress(min(pct, 1.0))
        st.markdown("<div style='margin-bottom:0.5rem;'></div>",
                    unsafe_allow_html=True)


def page_study_mode(user: dict):
    render_page_title(
        "Study Mode",
        "Run a focus block and log the time into today's plan.",
        "focus",
    )

    courses = db.list_courses(user["id"])
    if not courses:
        st.info("Add a course first, then come back here to study it.")
        return

    sessions_df = sessions_as_df(user["id"])
    today = dt.date.today()
    today_sessions = sessions_df[
        (sessions_df["session_date"] == today)
        & (sessions_df["planned_minutes"] > sessions_df["completed_minutes"])
    ].copy() if not sessions_df.empty else pd.DataFrame()

    targets = []
    for _, row in today_sessions.iterrows():
        remaining = int(row["planned_minutes"] - row["completed_minutes"])
        targets.append({
            "kind": "session",
            "label": f"📚 {row['course_name']}  —  {fmt_minutes(remaining)} remaining",
            "session_id": int(row["id"]),
            "course_id": int(row["course_id"]),
            "course_name": row["course_name"],
            "planned_minutes": int(row["planned_minutes"]),
            "completed_minutes": int(row["completed_minutes"]),
            "remaining_minutes": remaining,
        })

    targets += [{
        "kind": "course",
        "label": f"➕ Extra study — {c['name']}",
        "course_id": int(c["id"]),
        "course_name": c["name"],
        "remaining_minutes": 60,
    } for c in courses]

    if not targets:
        targets = [{
            "kind": "course",
            "label": f"➕ Extra study — {c['name']}",
            "course_id": int(c["id"]),
            "course_name": c["name"],
            "remaining_minutes": 60,
        } for c in courses]

    labels = [t["label"] for t in targets]
    # Reset stale selection (e.g. after a session's remaining-minutes label changes)
    if st.session_state.get("study_target_label") not in labels:
        st.session_state["study_target_label"] = labels[0]
    selected_label = st.selectbox(
        "What are you studying now?", labels, key="study_target_label")
    target = targets[labels.index(selected_label)]

    st.divider()

    mode = st.radio(
        "Timer style",
        ["Pomodoro (25 / 5)", "Ultradian (90 / 20)", "Custom"],
        horizontal=True)

    if mode.startswith("Pomodoro"):
        focus_min, break_min, label = 25, 5, "Pomodoro"
    elif mode.startswith("Ultradian"):
        focus_min, break_min, label = 90, 20, "Ultradian"
    else:
        label = "Custom"
        c1, c2 = st.columns(2)
        with c1:
            focus_min = st.number_input(
                "Focus minutes", 5, 180, 45, step=5)
        with c2:
            break_min = st.number_input(
                "Break minutes", 0, 60, 10, step=5)

    remaining = int(target.get("remaining_minutes", focus_min))
    recommended_rounds = max(1, min(12, -(-remaining // int(focus_min))))
    rounds = st.number_input(
        "Rounds for this study block",
        1, 12, int(recommended_rounds), step=1)
    total_focus = int(rounds * focus_min)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Course", target["course_name"])
    m2.metric("Remaining today",
              fmt_minutes(remaining) if target["kind"] == "session" else "Extra")
    m3.metric("Focus block", f"{int(focus_min)} min")
    m4.metric("Total focus", fmt_minutes(total_focus))

    if target["kind"] == "session":
        pct = min(target["completed_minutes"] / max(target["planned_minutes"], 1), 1.0)
        st.progress(pct)
        st.caption(
            f"{fmt_minutes(target['completed_minutes'])} completed / "
            f"{fmt_minutes(target['planned_minutes'])} planned today — "
            f"{pct*100:.0f}% done")
    else:
        st.info(
            "Extra study is logged as a completed bonus session for today "
            "and appears in your analytics.", icon="ℹ️")

    st.markdown(
        f"**{label} Mode** · {int(focus_min)} min focus / "
        f"{int(break_min)} min break · {int(rounds)} round"
        f"{'s' if rounds != 1 else ''} · {fmt_minutes(total_focus)} total")

    st.components.v1.html(
        _timer_html(int(focus_min), int(break_min), int(rounds)),
        height=430)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        block_label = (
            f"Log 1 focus block ({fmt_minutes(focus_min)})"
            if target["kind"] == "session"
            else f"Log 1 focus block ({fmt_minutes(focus_min)}) — extra"
        )
        if st.button(block_label, type="primary", use_container_width=True):
            logged = log_study_minutes(user["id"], target, int(focus_min))
            if logged > 0:
                st.toast(f"Logged {fmt_minutes(logged)} for {target['course_name']}.")
            else:
                st.toast("Nothing to log — session already at 0 minutes.")
            st.rerun()
    with c2:
        if target["kind"] == "session":
            remaining_now = (
                target["planned_minutes"] - target["completed_minutes"])
            btn_label = (
                f"Complete session ({fmt_minutes(remaining_now)} left)"
                if remaining_now > 0
                else "Session done ✓ — log extra time above"
            )
            if st.button(btn_label, use_container_width=True,
                         disabled=(remaining_now <= 0)):
                logged = log_study_minutes(user["id"], target, remaining_now)
                st.toast(
                    f"Session complete — {fmt_minutes(logged)} logged for "
                    f"{target['course_name']}.")
                st.rerun()
        else:
            all_rounds_label = (
                f"Log all {int(rounds)} round{'s' if rounds != 1 else ''} "
                f"({fmt_minutes(total_focus)})"
            )
            if st.button(all_rounds_label, use_container_width=True):
                logged = log_study_minutes(user["id"], target, total_focus)
                st.toast(
                    f"Logged {fmt_minutes(logged)} for {target['course_name']}.")
                st.rerun()

def log_study_minutes(user_id: int, target: dict, minutes: int) -> int:
    """Add completed focus time to a scheduled session or extra course study."""
    minutes = max(0, int(minutes))
    if minutes <= 0:
        return 0

    if target["kind"] == "session":
        planned = int(target["planned_minutes"])
        completed = int(target["completed_minutes"])
        if completed >= planned:
            # Session already complete — extend both planned and completed so
            # extra focus time is properly recorded in analytics.
            db.update_session_planned(user_id, int(target["session_id"]),
                                      planned + minutes)
            db.update_session_completed(user_id, int(target["session_id"]),
                                        completed + minutes)
            return minutes
        new_completed = min(planned, completed + minutes)
        logged = new_completed - completed
        if logged > 0:
            db.update_session_completed(
                user_id, int(target["session_id"]), new_completed)
        return logged

    today = dt.date.today()
    sessions = db.list_sessions(user_id)
    existing = next(
        (s for s in sessions
         if int(s["course_id"]) == int(target["course_id"])
         and s["session_date"] == today),
        None,
    )
    if existing:
        new_planned = int(existing["planned_minutes"]) + minutes
        new_completed = int(existing["completed_minutes"]) + minutes
        db.update_session_planned(user_id, int(existing["id"]), new_planned)
        db.update_session_completed(user_id, int(existing["id"]), new_completed)
    else:
        db.insert_session(
            user_id, int(target["course_id"]), today,
            planned_minutes=minutes, completed_minutes=minutes)
    return minutes


def _timer_html(focus_min: int, break_min: int, rounds: int = 1) -> str:
    return f"""
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        .tc {{
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
            text-align:center; padding:30px 20px;
            background:#ffffff; color:#111111; max-width:420px; margin:0 auto;
            border:1px solid rgba(17,17,17,0.12); border-radius:24px;
            box-shadow:0 16px 44px rgba(17,17,17,0.06);
        }}
        .td {{ font-size:4.6em; font-weight:800; letter-spacing:0;
               margin:16px 0; color:#111111;
               transition:color 0.4s; }}
        .td.brk {{ color:#555555; }}
        .sl {{ font-size:1.1em; font-weight:700; text-transform:uppercase;
               letter-spacing:0.08em; margin-bottom:8px; min-height:1.5em; }}
        .sl.f {{ color:#111111; }} .sl.b {{ color:#555555; }}
        .sl.p {{ color:#555555; }}
        .br {{ display:flex; gap:12px; justify-content:center; margin-top:22px; }}
        .btn {{ padding:10px 28px; border:1px solid rgba(17,17,17,0.14);
                border-radius:999px;
                font-family:inherit; font-size:1em; font-weight:700;
                cursor:pointer; transition:transform 0.1s; }}
        .btn:hover {{ transform:scale(1.05); }}
        .btn:active {{ transform:scale(0.97); }}
        .btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
        .bs {{ background:#111111; color:white; }}
        .bp {{ background:#f3f3f3; color:#111111; }}
        .bx {{ background:#ffffff; color:#111111; }}
        .pb {{ width:100%; height:6px; background:#e6e6e6;
               margin-top:18px; overflow:hidden; border-radius:999px; }}
        .pf {{ height:100%;
               transition:width 1s linear, background 0.4s;
               background:#111111; }}
        .pf.brk {{ background:#555555; }}
        .sc {{ font-size:0.85em; color:#555555; margin-top:14px; }}
    </style>
    <div class="tc">
        <div class="sl" id="sL">Ready</div>
        <div class="td" id="tD">{focus_min:02d}:00</div>
        <div class="pb"><div class="pf" id="pB" style="width:0%"></div></div>
        <div class="sc" id="sC">Focus rounds completed: 0 / {rounds}</div>
        <div class="br">
            <button class="btn bs" id="bS" onclick="go()">Start</button>
            <button class="btn bp" id="bP" onclick="pa()" disabled>Pause</button>
            <button class="btn bx" onclick="re()">Reset</button>
        </div>
    </div>
    <script>
    const FS={focus_min*60},BS={break_min*60},TR={rounds};
    let rm=FS,ts=FS,iv=null,ib=false,sn=0;
    const tD=document.getElementById('tD'),sL=document.getElementById('sL'),
          pB=document.getElementById('pB'),bS=document.getElementById('bS'),
          bP=document.getElementById('bP'),sC=document.getElementById('sC');
    function rd(){{
        const m=Math.floor(rm/60),s=rm%60;
        tD.textContent=String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
        pB.style.width=((ts-rm)/ts*100).toFixed(1)+'%';
        tD.className=ib?'td brk':'td';
        pB.className=ib?'pf brk':'pf';
    }}
    function tk(){{
        rm--;
        if(rm<0){{
            clearInterval(iv);iv=null;
            if(!ib){{sn++;
                sC.textContent='Focus rounds completed: '+sn+' / '+TR;
                if(sn>=TR){{
                    sL.textContent='Complete';sL.className='sl b';
                    bS.disabled=true;bP.disabled=true;rm=0;rd();return;
                }}
                ib=true;rm=BS;ts=BS;sL.textContent='Break';sL.className='sl b';
            }}else{{ib=false;rm=FS;ts=FS;sL.textContent='Focus';sL.className='sl f';}}
            rd();go();return;
        }}
        rd();
    }}
    function go(){{if(iv)return;
        sL.textContent=ib?'Break':'Focus';
        sL.className=ib?'sl b':'sl f';
        iv=setInterval(tk,1000);bS.disabled=true;bP.disabled=false;}}
    function pa(){{if(iv){{clearInterval(iv);iv=null;
        sL.textContent='Paused';sL.className='sl p';
        bS.disabled=false;bP.disabled=true;}}}}
    function re(){{clearInterval(iv);iv=null;ib=false;rm=FS;ts=FS;sn=0;
        sL.textContent='Ready';sL.className='sl';
        sC.textContent='Focus rounds completed: 0 / '+TR;
        bS.disabled=false;bP.disabled=true;rd();}}
    rd();
    </script>
    """


def page_profile(user: dict):
    render_page_title(
        "Profile",
        "Account settings and local data controls.",
        "account",
    )

    st.markdown(
        f"""
        <div class="nova-sidebar-meta">
            <div class="hello">Signed in as</div>
            <div class="name">{h(user['display_name'])}
                <span style="color:var(--muted);font-weight:400;">
                    @{h(user['username'])}
                </span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("Edit profile")
    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            display = st.text_input("Display name", user["display_name"])
        with c2:
            email = st.text_input("Email", user.get("email") or "")

        countries = api.list_supported_countries()
        codes = [c[0] for c in countries]
        current_code = user.get("country_code", DEFAULT_COUNTRY)
        country_idx = codes.index(current_code) if current_code in codes else 0
        country = st.selectbox(
            "Country", codes,
            format_func=lambda c: next(
                (f"{cc} - {name}" for cc, name in countries if cc == c), c),
            index=country_idx,
            help="Used for public holidays when 'Skip holidays' is enabled.")

        if st.form_submit_button("Save profile", type="primary"):
            if not auth.is_valid_email(email):
                st.error("Please enter a valid email address.")
            else:
                db.update_user_profile(
                    user["id"],
                    display_name=display.strip(),
                    email=(email.strip() or None),
                    country_code=country,
                )
                st.toast("Profile updated.")
                st.rerun()

    st.divider()

    st.subheader("Change password")
    with st.form("pw_form", clear_on_submit=True):
        cur_pw = st.text_input("Current password", type="password")
        new_pw = st.text_input(
            "New password",
            type="password",
            help="At least 8 characters, with letters and digits",
        )
        new_pw2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password"):
            if new_pw != new_pw2:
                st.error("New passwords don't match.")
            else:
                ok, msg = auth.change_password(user["id"], cur_pw, new_pw)
                (st.success if ok else st.error)(msg)

    st.divider()

    with st.expander("Danger zone"):
        st.caption(
            "Wipe all your courses, sessions, and generated plan.  "
            "Your account and settings are kept.")
        if st.button("Clear my study data", type="secondary"):
            db.clear_user_data(user["id"])
            st.toast("All courses and sessions deleted.")
            st.rerun()


def page_export(user: dict):
    render_page_title(
        "Export & Import",
        "Download your schedule or bring courses in from a CSV.",
        "files",
    )

    sessions_df = sessions_as_df(user["id"])
    courses = db.list_courses(user["id"])

    # ── Export study plan ─────────────────────────────────────────────────
    st.subheader("Export study plan")
    if sessions_df.empty:
        st.info("Generate a plan first to enable exports.")
    else:
        total_sessions = len(sessions_df)
        total_hours = fmt_minutes(int(sessions_df["planned_minutes"].sum()))
        st.markdown(
            f'<div style="background:var(--panel);border:1px solid var(--line);'
            f'border-radius:var(--radius-md);padding:1rem 1.2rem;'
            f'margin-bottom:0.75rem;">'
            f'<strong>{total_sessions} sessions</strong> · '
            f'<strong>{total_hours}</strong> of planned study time'
            f'</div>',
            unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "📄  Download as CSV",
                sessions_df.to_csv(index=False),
                file_name=f"nova_plan_{user['username']}.csv",
                mime="text/csv", use_container_width=True,
                help="Open in Excel, Google Sheets, or any spreadsheet app.")
        with c2:
            ics_bytes = _build_ics(sessions_df, courses, user)
            st.download_button(
                "📅  Download as Calendar (.ics)",
                ics_bytes,
                file_name=f"nova_plan_{user['username']}.ics",
                mime="text/calendar", use_container_width=True,
                help="Import into Google Calendar, Outlook, or Apple Calendar.")

    st.divider()

    # ── Export courses ────────────────────────────────────────────────────
    st.subheader("Export courses")
    if not courses:
        st.info("No courses to export yet.")
    else:
        cdf = pd.DataFrame([
            {"name": c["name"],
             "exam_date": c["exam_date"].isoformat(),
             "ects": c["ects"],
             "difficulty": c["difficulty"],
             "estimated_hours": c["estimated_hours"]}
            for c in courses
        ])
        st.caption(
            f"{len(courses)} course{'s' if len(courses) != 1 else ''} · "
            "CSV format compatible with the import tool below.")
        st.download_button(
            "📋  Download courses (CSV)",
            cdf.to_csv(index=False),
            file_name=f"nova_courses_{user['username']}.csv",
            mime="text/csv", use_container_width=True)

    st.divider()

    # ── Import courses ────────────────────────────────────────────────────
    st.subheader("Import courses")
    st.caption(
        "Upload a CSV with columns: `name, exam_date (YYYY-MM-DD), ects, "
        "difficulty (1–5), estimated_hours`. "
        "Existing courses with the same name will be updated.")
    uploaded = st.file_uploader(
        "Choose a CSV file", type=["csv"], label_visibility="collapsed")
    if uploaded:
        try:
            df = pd.read_csv(uploaded, parse_dates=["exam_date"])
            imported = 0
            for _, r in df.iterrows():
                exam_d = r["exam_date"]
                if hasattr(exam_d, "date"):
                    exam_d = exam_d.date()
                existing = next(
                    (c for c in db.list_courses(user["id"])
                     if c["name"] == str(r["name"])), None)
                db.upsert_course(
                    user["id"],
                    name=str(r["name"]).strip(),
                    exam_date=exam_d,
                    ects=float(r["ects"]),
                    difficulty=int(r["difficulty"]),
                    estimated_hours=float(r["estimated_hours"]),
                    course_id=existing["id"] if existing else None,
                )
                imported += 1
            st.success(f"Imported/updated {imported} courses.")
        except Exception as e:
            st.error(f"Import failed: {e}")


def _build_ics(sessions_df: pd.DataFrame, courses: list[dict],
               user: dict) -> bytes:
    """Produce a minimal valid ICS feed of all sessions + exam events."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Nova Exam Planner//{user['username']}//EN",
        "CALSCALE:GREGORIAN",
    ]
    now = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for _, r in sessions_df.iterrows():
        d = r["session_date"]
        ds = d.strftime("%Y%m%d")
        dnext = (d + dt.timedelta(days=1)).strftime("%Y%m%d")
        duration_h = int(r["planned_minutes"]) / 60
        uid = f"nova-sess-{r['id']}@nova"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{ds}",
            f"DTEND;VALUE=DATE:{dnext}",
            f"SUMMARY:Study: {r['course_name']} "
            f"({duration_h:.1f}h)",
            f"DESCRIPTION:Planned {int(r['planned_minutes'])} min "
            f"via Nova Exam Planner.",
            "END:VEVENT",
        ]

    for c in courses:
        ds = c["exam_date"].strftime("%Y%m%d")
        dnext = (c["exam_date"] + dt.timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:nova-exam-{c['id']}@nova",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{ds}",
            f"DTEND;VALUE=DATE:{dnext}",
            f"SUMMARY:Exam: {c['name']}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode("utf-8")


PAGES = [
    ("Dashboard", page_dashboard),
    ("Courses", page_courses),
    ("Study Plan", page_study_plan),
    ("Customize", page_customize),
    ("Analytics", page_analytics),
    ("Study Mode", page_study_mode),
    ("Export", page_export),
    ("Cafeteria", page_cafeteria),
    ("Profile", page_profile),
]
PAGE_NAMES = [name for name, _ in PAGES]
PAGE_BY_NAME = dict(PAGES)


def render_sidebar(user: dict) -> str:
    with st.sidebar:
        st.markdown(
            f"""
            <div class="nova-sidebar-logo">
                <img src="{logo_data_uri()}" alt="Nova SBE" />
            </div>
            <div class="nova-sidebar-meta">
                <div class="hello">Signed in as</div>
                <div class="name">{h(user['display_name'])}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()
        if st.session_state.get("page_choice") not in PAGE_NAMES:
            st.session_state["page_choice"] = "Dashboard"
        choice = st.radio(
            "Navigation",
            PAGE_NAMES,
            key="page_choice",
            label_visibility="collapsed",
        )
        st.divider()

        n_courses = len(db.list_courses(user["id"]))
        st.caption(f"{n_courses} course{'s' if n_courses != 1 else ''}")
        if db.has_plan(user["id"]):
            df = sessions_as_df(user["id"])
            total = int(df["planned_minutes"].sum())
            done = int(df["completed_minutes"].sum())
            st.caption(f"{fmt_minutes(total)} planned")
            st.caption(f"{fmt_minutes(done)} done")

        st.divider()
        if st.button("Log out", use_container_width=True):
            auth.logout()
            st.rerun()

        st.caption(APP_TITLE)
        return choice


def main():
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=str(NOVA_FAVICON_PATH),
        layout="wide",
        initial_sidebar_state="expanded",
    )
    db.init_db()
    apply_theme()

    user = auth.current_user()
    if not user:
        page_auth()
        return

    apply_pending_page_choice(PAGE_NAMES)
    choice = render_sidebar(user)
    render_adaptive_rescheduler(user)
    PAGE_BY_NAME[choice](user)


if __name__ == "__main__":
    main()
