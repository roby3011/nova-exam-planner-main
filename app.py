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
                    help="At least 8 characters, with letters and digits",
                )
                rp2 = st.text_input("Confirm password *", type="password")
                reg_submit = st.form_submit_button(
                    "Create account", type="primary",
                    use_container_width=True)

            if reg_submit:
                if rp != rp2:
                    st.error("Passwords don't match.")
                else:
                    uid, err = auth.register_user(
                        ru.strip(), re_mail.strip(), rp,
                        display_name=rd.strip() or None,
                        country_code=country_code,
                    )
                    if err:
                        st.error(err)
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

    preferred_days = st.multiselect(
        "Preferred study days",
        DAY_NAMES,
        default=con["preferred_days"] or DAY_NAMES,
    )
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
            future = [h for h in holidays if h["date"] >= dt.date.today()][:12]
            if not future:
                st.caption("No upcoming holidays found.")
            else:
                for h in future:
                    st.markdown(
                        f"- **{h['date']:%d %b %Y}** - "
                        f"{h['local_name']} ({h['name']})")

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

    with st.form("course_form", clear_on_submit=True):
        fc1, fc2 = st.columns(2)
        with fc1:
            name = st.text_input("Course name", defaults["name"])
            ects = st.number_input(
                "ECTS", 0.5, 30.0, float(defaults["ects"]),
                step=0.5, format="%.1f")
        with fc2:
            exam_date = st.date_input("Exam date", defaults["exam_date"])
            difficulty = st.slider(
                "Difficulty (1-5)", 1, 5, int(defaults["difficulty"]))

        auto_est = estimate_hours(ects, difficulty)
        estimated = st.number_input(
            f"Estimated study hours (suggested: {fmt_hours(auto_est)})",
            min_value=1.0,
            value=(defaults["estimated_hours"]
                   if defaults["estimated_hours"] > 0 else auto_est),
            step=0.5,
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
            final_name = separated_name if duplicate_name and separate_duplicate \
                else name.strip()
            if duplicate_name and not separate_duplicate:
                st.error(
                    "That course name already exists. Tick the checkbox to "
                    "save a separated copy, or choose a different name."
                )
                return
            try:
                db.upsert_course(
                    user["id"], final_name, exam_date,
                    ects, difficulty, estimated,
                    course_id=editing_id,
                )
                st.session_state["editing_course_id"] = None
                st.toast(f"{'Updated' if editing else 'Added'} {final_name}.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save course: {e}")

    if editing and st.button("Cancel editing"):
        st.session_state["editing_course_id"] = None
        st.rerun()

    st.divider()

    st.subheader("Your courses")
    courses = db.list_courses(user["id"])
    if not courses:
        st.info("No courses yet. Add one above to get started.")
        return

    for c in courses:
        days_until = (c["exam_date"] - dt.date.today()).days
        status = "Due soon" if days_until <= 7 else (
            "Coming up" if days_until <= 21 else "Later"
        )

        with st.container(border=True):
            cols = st.columns([0.30, 0.10, 0.10, 0.16, 0.14, 0.10, 0.10])
            cols[0].markdown(f"**{c['name']}**")
            cols[1].caption(f"ECTS {c['ects']:g}")
            cols[2].caption(f"Diff {c['difficulty']}/5")
            cols[3].caption(f"Est. {fmt_hours(c['estimated_hours'])}")
            cols[4].caption(f"{c['exam_date']:%d %b}")
            cols[5].caption(
                f"{status}: {days_until}d" if days_until >= 0 else "past")
            with cols[6]:
                b1, b2 = st.columns(2)
                if b1.button("Edit", key=f"edit_{c['id']}",
                             help="Edit this course"):
                    st.session_state["editing_course_id"] = c["id"]
                    st.rerun()
                if b2.button("Del", key=f"del_{c['id']}",
                             help="Delete this course"):
                    db.delete_course(user["id"], c["id"])
                    st.toast(f"Deleted {c['name']}.")
                    st.rerun()

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
        "Edit generated sessions and rebalance a course when needed.",
        "edit",
    )

    sessions_df = sessions_as_df(user["id"])
    if sessions_df.empty:
        st.info("Generate a plan on the Courses page first.")
        return

    st.caption(
        "Edit **Planned Minutes** for any session, then **Save** or "
        "**Rebalance** one course's diff across its remaining future "
        "sessions so its total stays on target.")

    edit_df = sessions_df[[
        "id", "session_date", "course_name", "planned_minutes",
    ]].rename(columns={"session_date": "date"}).copy()
    edit_df["planned_minutes"] = edit_df["planned_minutes"].astype(int)

    c_filter, w_filter = st.columns(2)
    course_names = sorted(edit_df["course_name"].unique())
    with c_filter:
        sel_courses = st.multiselect(
            "Filter by course", course_names, default=course_names)
    filtered = edit_df[edit_df["course_name"].isin(sel_courses)]

    with w_filter:
        if not filtered.empty:
            weeks = sorted(
                pd.to_datetime(filtered["date"])
                  .dt.isocalendar().week.unique())
            if len(weeks) > 1:
                wk = st.select_slider(
                    "Filter by calendar week",
                    options=weeks, value=(weeks[0], weeks[-1]))
                mask_week = pd.to_datetime(
                    filtered["date"]).dt.isocalendar().week.between(*wk)
                filtered = filtered[mask_week]

    st.divider()

    edited = st.data_editor(
        filtered,
        column_config={
            "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
            "date": st.column_config.DateColumn("Date", disabled=True),
            "course_name": st.column_config.TextColumn(
                "Course", disabled=True),
            "planned_minutes": st.column_config.NumberColumn(
                "Planned Minutes", min_value=0, max_value=480, step=5),
        },
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="plan_editor",
    )

    st.divider()

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save edits", use_container_width=True,
                     type="primary"):
            for _, row in edited.iterrows():
                db.update_session_planned(
                    user["id"], int(row["id"]), int(row["planned_minutes"]))
            st.toast("Edits saved.")
            st.rerun()

    with c2:
        rebalance_course = st.selectbox(
            "Course to rebalance", course_names,
            label_visibility="collapsed")
        if st.button("Rebalance course", use_container_width=True):
            course = next(
                (c for c in db.list_courses(user["id"])
                 if c["name"] == rebalance_course), None)
            if course:
                sdf = sessions_as_df(user["id"])
                sdf = rebalance_course_sessions(
                    sdf.copy(), course["id"],
                    course["estimated_hours"] * 60)
                for _, row in sdf.iterrows():
                    db.update_session_planned(
                        user["id"], int(row["id"]),
                        int(row["planned_minutes"]))
                st.toast(f"Rebalanced {rebalance_course}.")
                st.rerun()

    st.divider()
    st.subheader("Plan Summary")
    sessions_df = sessions_as_df(user["id"])
    totals = courses_total_minutes(user["id"])
    per_course = (
        sessions_df.groupby(["course_id", "course_name"])["planned_minutes"]
        .sum().reset_index()
    )
    for _, r in per_course.iterrows():
        target = totals.get(int(r["course_id"]), 0)
        diff = int(r["planned_minutes"]) - int(target)
        status = "OK" if abs(diff) < 10 else "Review"
        diff_str = (f"+{fmt_minutes(diff)}" if diff >= 0
                    else f"-{fmt_minutes(abs(diff))}")
        st.markdown(
            f"**{status}: {r['course_name']}** - "
            f"Planned: {fmt_minutes(int(r['planned_minutes']))} | "
            f"Target: {fmt_minutes(target)} | Diff: {diff_str}")


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

    st.divider()

    courses = db.list_courses(user["id"])
    all_names = [c["name"] for c in courses]
    cmap = {n: course_color(n, all_names) for n in all_names}

    st.subheader("Study time per course")
    pc = a["per_course"].sort_values("planned", ascending=True).copy()
    pc["label"] = pc["planned"].apply(fmt_minutes)
    fig1 = px.bar(
        pc, x="planned", y="course_name", orientation="h",
        color="course_name", color_discrete_map=cmap, text="label")
    fig1.update_traces(textposition="outside")
    fig1.update_layout(
        showlegend=False,
        height=max(250, len(pc) * 60),
        margin=dict(l=0, r=80, t=10, b=0),
        xaxis_title="Planned Minutes", yaxis_title="")
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
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=-0.25))
    st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    st.subheader("Daily load")
    daily = a["daily"].copy()
    daily["hours"] = daily["total_minutes"] / 60
    fig3 = px.area(
        daily, x="date", y="hours",
        color_discrete_sequence=["#111111"],
        labels={"date": "Date", "hours": "Hours"})
    fig3.add_hline(
        y=int(con["max_hours_per_day"]),
        line_dash="dash", line_color="#555555",
        annotation_text="Daily max")
    fig3.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    st.subheader("Completed vs remaining")
    pc2 = a["per_course"].copy()
    fig4 = go.Figure()
    fig4.add_trace(go.Bar(
        name="Completed", x=pc2["course_name"],
        y=pc2["completed"] / 60, marker_color="#111111"))
    fig4.add_trace(go.Bar(
        name="Remaining", x=pc2["course_name"],
        y=pc2["remaining"] / 60, marker_color="#e9ecef"))
    fig4.update_layout(
        barmode="stack", height=380,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Hours",
        legend=dict(orientation="h", y=-0.15))
    st.plotly_chart(fig4, use_container_width=True)

    st.divider()

    st.subheader("Completion progress")
    for _, r in a["per_course"].iterrows():
        pct = r["pct"] / 100
        st.markdown(
            f"**{r['course_name']}** - {fmt_minutes(r['completed'])} / "
            f"{fmt_minutes(r['planned'])}  ({r['pct']:.0f}%)")
        st.progress(min(pct, 1.0))

    st.metric("Overall completion",
              f"{a['pct']:.1f}%",
              f"{fmt_minutes(a['total_completed'])} / "
              f"{fmt_minutes(a['total_planned'])}")


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
            "label": (
                f"{row['course_name']} - {fmt_minutes(remaining)} "
                "remaining today"
            ),
            "session_id": int(row["id"]),
            "course_id": int(row["course_id"]),
            "course_name": row["course_name"],
            "planned_minutes": int(row["planned_minutes"]),
            "completed_minutes": int(row["completed_minutes"]),
            "remaining_minutes": remaining,
        })

    targets += [{
        "kind": "course",
        "label": f"Extra study - {c['name']}",
        "course_id": int(c["id"]),
        "course_name": c["name"],
        "remaining_minutes": 60,
    } for c in courses]

    labels = [t["label"] for t in targets]
    selected_label = st.selectbox("What are you studying now?", labels)
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
    m1.metric("Target", target["course_name"])
    m2.metric("Remaining", fmt_minutes(remaining)
              if target["kind"] == "session" else "Extra study")
    m3.metric("Focus block", f"{int(focus_min)} min")
    m4.metric("Planned focus", fmt_minutes(total_focus))

    if target["kind"] == "session":
        st.progress(
            min(target["completed_minutes"] / target["planned_minutes"], 1.0))
        st.caption(
            f"{fmt_minutes(target['completed_minutes'])} completed out of "
            f"{fmt_minutes(target['planned_minutes'])} planned today.")
    else:
        st.caption(
            "Extra study is logged as a completed session for today, so it "
            "appears in your analytics and progress totals.")

    st.markdown(
        f"**{label} Mode** - {int(focus_min)} min focus / "
        f"{int(break_min)} min break / {int(rounds)} round"
        f"{'s' if rounds != 1 else ''}")

    st.components.v1.html(
        _timer_html(int(focus_min), int(break_min), int(rounds)),
        height=430)

    c1, c2 = st.columns(2)
    with c1:
        if st.button(
            f"Add one focus block ({fmt_minutes(focus_min)})",
            type="primary",
            use_container_width=True,
        ):
            logged = log_study_minutes(user["id"], target, int(focus_min))
            st.toast(f"Logged {fmt_minutes(logged)} for {target['course_name']}.")
            st.rerun()
    with c2:
        if target["kind"] == "session":
            if st.button("Mark selected session complete",
                         use_container_width=True):
                remaining_now = (
                    target["planned_minutes"] - target["completed_minutes"])
                if remaining_now > 0:
                    logged = log_study_minutes(user["id"], target, remaining_now)
                    st.toast(
                        f"Completed {target['course_name']} "
                        f"({fmt_minutes(logged)} logged).")
                    st.rerun()
        else:
            if st.button(
                f"Log all {int(rounds)} round"
                f"{'s' if rounds != 1 else ''}",
                use_container_width=True,
            ):
                logged = log_study_minutes(user["id"], target, total_focus)
                st.toast(f"Logged {fmt_minutes(logged)} for {target['course_name']}.")
                st.rerun()

def log_study_minutes(user_id: int, target: dict, minutes: int) -> int:
    """Add completed focus time to a scheduled session or extra course study."""
    minutes = max(0, int(minutes))
    if minutes <= 0:
        return 0

    if target["kind"] == "session":
        planned = int(target["planned_minutes"])
        completed = int(target["completed_minutes"])
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
        "Move courses in or take your schedule out.",
        "files",
    )

    sessions_df = sessions_as_df(user["id"])
    courses = db.list_courses(user["id"])

    if not sessions_df.empty:
        st.subheader("Export study plan")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Download as CSV",
                sessions_df.to_csv(index=False),
                file_name=f"nova_plan_{user['username']}.csv",
                mime="text/csv", use_container_width=True)
        with c2:
            ics_bytes = _build_ics(sessions_df, courses, user)
            st.download_button(
                "Download as ICS (calendar)",
                ics_bytes,
                file_name=f"nova_plan_{user['username']}.ics",
                mime="text/calendar", use_container_width=True,
                help="Import into Google Calendar, Outlook, Apple Calendar.")

    if courses:
        st.subheader("Export courses")
        cdf = pd.DataFrame([
            {"name": c["name"],
             "exam_date": c["exam_date"].isoformat(),
             "ects": c["ects"],
             "difficulty": c["difficulty"],
             "estimated_hours": c["estimated_hours"]}
            for c in courses
        ])
        st.download_button(
            "Download courses (CSV)",
            cdf.to_csv(index=False),
            file_name=f"nova_courses_{user['username']}.csv",
            mime="text/csv", use_container_width=True)

    st.divider()

    st.subheader("Import courses")
    st.caption(
        "Columns: `name, exam_date, ects, difficulty, estimated_hours`. "
        "Existing courses with the same name will be updated.")
    uploaded = st.file_uploader("Upload CSV", type=["csv"])
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
    ("Cafeteria", page_cafeteria),
    ("Customize", page_customize),
    ("Analytics", page_analytics),
    ("Study Mode", page_study_mode),
    ("Export", page_export),
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
