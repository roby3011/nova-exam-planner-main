"""Study-plan scheduling helpers."""

import datetime as dt
from typing import Iterable, Optional

import pandas as pd

DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


def _available_days(start: dt.date, end: dt.date,
                    preferred: set[str],
                    exclude: Optional[set[dt.date]] = None) -> list[dt.date]:
    """Study days before an exam."""
    exclude = exclude or set()
    out, cur = [], start
    while cur < end:
        if DAY_NAMES[cur.weekday()] in preferred and cur not in exclude:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _week_start(day: dt.date) -> dt.date:
    """Monday of the calendar week containing ``day``."""
    return day - dt.timedelta(days=day.weekday())


def _floor_to_5(minutes: float) -> int:
    """Round down to the nearest 5-minute block."""
    return max(0, int(minutes // 5) * 5)


def _trim_items_to_cap(items: dict, cap: float) -> dict:
    """Trim values to a cap in 5-minute blocks."""
    total = sum(items.values())
    if total <= cap:
        return dict(items)

    scale = cap / total if total else 0
    trimmed = {key: _floor_to_5(value * scale)
               for key, value in items.items()}

    # Put spare blocks back where trimming hurt most.
    spare = _floor_to_5(cap - sum(trimmed.values()))
    candidates = sorted(
        items,
        key=lambda key: items[key] - trimmed[key],
        reverse=True,
    )
    for key in candidates:
        if spare < 5:
            break
        if trimmed[key] + 5 <= items[key]:
            trimmed[key] += 5
            spare -= 5

    return trimmed


def _used_on_day(course_allocs: dict[int, dict], day: dt.date) -> float:
    return sum(v["alloc"].get(day, 0) for v in course_allocs.values())


def _used_in_week(course_allocs: dict[int, dict], day: dt.date) -> float:
    week = _week_start(day)
    return sum(
        mins
        for v in course_allocs.values()
        for d, mins in v["alloc"].items()
        if _week_start(d) == week
    )


def generate_study_plan(courses: list[dict], *,
                        preferred_days: Iterable[str],
                        max_hours_per_day: int,
                        start_date: dt.date,
                        weekly_hours: Optional[float] = None,
                        exclude_dates: Optional[Iterable[dt.date]] = None
                        ) -> list[dict]:
    """Build study sessions that respect daily and weekly limits."""
    if not courses:
        return []

    preferred = set(preferred_days) or set(DAY_NAMES)
    if not preferred:
        raise ValueError("preferred_days must contain at least one day")
    max_hours_per_day = max(1, int(max_hours_per_day))
    if weekly_hours is not None:
        weekly_hours = max(1.0, float(weekly_hours))
    if not isinstance(start_date, dt.date):
        raise TypeError(f"start_date must be a datetime.date, got {type(start_date)}")

    valid_courses = []
    for c in courses:
        if float(c.get("estimated_hours", 0)) <= 0:
            continue
        exam_d = c.get("exam_date")
        if isinstance(exam_d, str):
            exam_d = dt.date.fromisoformat(exam_d)
        if exam_d <= start_date:
            continue
        valid_courses.append(c)
    if not valid_courses:
        return []
    courses = valid_courses

    max_daily = max_hours_per_day * 60
    max_weekly = weekly_hours * 60 if weekly_hours else None
    exclude = set(exclude_dates) if exclude_dates else set()

    # First pass: spread each course across its study days.
    course_allocs: dict[int, dict] = {}
    for c in courses:
        total = c["estimated_hours"] * 60
        exam_d = c["exam_date"]
        if isinstance(exam_d, str):
            exam_d = dt.date.fromisoformat(exam_d)
        avail = _available_days(start_date, exam_d, preferred, exclude)
        if not avail:
            continue
        daily = total / len(avail)
        daily = round(daily / 5) * 5
        if daily < 5 and total >= 5:
            daily = 5
        course_allocs[c["id"]] = {
            "name": c["name"],
            "exam_date": exam_d,
            "alloc": {d: daily for d in avail},
        }

    # Trim anything that breaks daily or weekly limits.
    all_days = sorted({d for v in course_allocs.values() for d in v["alloc"]})
    overflow = {cid: 0 for cid in course_allocs}

    for day in all_days:
        items = {cid: v["alloc"][day] for cid, v in course_allocs.items()
                 if v["alloc"].get(day, 0) > 0}
        day_total = sum(items.values())
        if day_total <= max_daily:
            continue
        trimmed_items = _trim_items_to_cap(items, max_daily)
        for cid, val in items.items():
            trimmed = trimmed_items[cid]
            course_allocs[cid]["alloc"][day] = trimmed
            overflow[cid] += val - trimmed

    if max_weekly:
        all_weeks = sorted({_week_start(day) for day in all_days})
        for week in all_weeks:
            items = {
                (cid, day): mins
                for cid, v in course_allocs.items()
                for day, mins in v["alloc"].items()
                if _week_start(day) == week and mins > 0
            }
            week_total = sum(items.values())
            if week_total <= max_weekly:
                continue
            trimmed_items = _trim_items_to_cap(items, max_weekly)
            for (cid, day), val in items.items():
                trimmed = trimmed_items[(cid, day)]
                course_allocs[cid]["alloc"][day] = trimmed
                overflow[cid] += val - trimmed

    # Put overflow back where there is still room.
    ordered = sorted(course_allocs.items(), key=lambda kv: kv[1]["exam_date"])
    for cid, v in ordered:
        remaining = overflow[cid]
        if remaining <= 0:
            continue
        avail = _available_days(start_date, v["exam_date"], preferred, exclude)
        for day in avail:
            if remaining <= 0:
                break
            daily_headroom = max_daily - _used_on_day(course_allocs, day)
            weekly_headroom = (max_weekly - _used_in_week(course_allocs, day)
                               if max_weekly else daily_headroom)
            headroom = min(daily_headroom, weekly_headroom)
            if headroom <= 0:
                continue
            add = _floor_to_5(min(remaining, headroom))
            if add <= 0:
                continue
            v["alloc"][day] = v["alloc"].get(day, 0) + add
            remaining -= add

    sessions: list[dict] = []
    for cid, v in course_allocs.items():
        for day, mins in sorted(v["alloc"].items()):
            if mins > 0:
                sessions.append({
                    "course_id": cid,
                    "course_name": v["name"],
                    "session_date": day,
                    "planned_minutes": int(mins),
                    "completed_minutes": 0,
                })
    return sessions

def rebalance_course_sessions(sessions_df: pd.DataFrame,
                              course_id: int,
                              target_total_minutes: float,
                              today: Optional[dt.date] = None) -> pd.DataFrame:
    """Redistribute one course after a manual edit."""
    if today is None:
        today = dt.date.today()
    if sessions_df.empty:
        return sessions_df

    mask = sessions_df["course_id"] == course_id
    rows = sessions_df.loc[mask].copy()
    if rows.empty:
        return sessions_df

    rows["_date"] = pd.to_datetime(rows["session_date"]).dt.date
    eligible = (rows["completed_minutes"] == 0) & (rows["_date"] >= today)
    eligible_total = rows.loc[eligible, "planned_minutes"].sum()
    current_total  = rows["planned_minutes"].sum()
    locked_total   = current_total - eligible_total

    need = target_total_minutes - locked_total
    if need <= 0 or eligible.sum() == 0:
        return sessions_df

    per_row = max(round(need / eligible.sum() / 5) * 5, 5)
    remainder = need

    for idx in rows.loc[eligible].index:
        alloc = min(per_row, remainder)
        alloc = max(round(alloc / 5) * 5, 0)
        sessions_df.at[idx, "planned_minutes"] = int(alloc)
        remainder -= alloc

    # Put the final remainder on the last eligible row.
    idxs = rows.loc[eligible].index.tolist()
    if remainder > 0 and idxs:
        sessions_df.at[idxs[-1], "planned_minutes"] += int(round(remainder / 5) * 5)

    return sessions_df


def compute_analytics(sessions_df: pd.DataFrame) -> dict:
    """Aggregate the sessions DataFrame into chart-ready pieces."""
    if sessions_df.empty:
        return {
            "total_planned": 0, "total_completed": 0,
            "total_remaining": 0, "total_days": 0, "pct": 0,
            "per_course": pd.DataFrame(
                columns=["course_name", "planned", "completed",
                         "remaining", "pct"]),
            "weekly": pd.DataFrame(),
            "week_order": [],
            "daily": pd.DataFrame(),
        }

    p = sessions_df.copy()
    p["date"] = pd.to_datetime(p["session_date"])

    total_planned   = int(p["planned_minutes"].sum())
    total_completed = int(p["completed_minutes"].sum())
    total_days      = int(p["date"].nunique())
    pct = (total_completed / total_planned * 100) if total_planned else 0

    per_course = (
        p.groupby("course_name")
         .agg(planned=("planned_minutes", "sum"),
              completed=("completed_minutes", "sum"))
         .reset_index()
    )
    per_course["remaining"] = per_course["planned"] - per_course["completed"]
    per_course["pct"] = (
        per_course["completed"] / per_course["planned"] * 100
    ).fillna(0)

    p["week_label"] = p["date"].apply(
        lambda d: (d - pd.Timedelta(days=d.weekday())).strftime("%d %b"))
    week_order = p.sort_values("date")["week_label"].unique().tolist()

    weekly = (
        p.groupby(["week_label", "course_name"])["planned_minutes"]
         .sum().reset_index()
    )

    daily = (
        p.groupby("date")["planned_minutes"].sum()
         .reset_index()
         .rename(columns={"planned_minutes": "total_minutes"})
    )

    return {
        "total_planned":   total_planned,
        "total_completed": total_completed,
        "total_remaining": total_planned - total_completed,
        "total_days":      total_days,
        "pct":             pct,
        "per_course":      per_course,
        "weekly":          weekly,
        "week_order":      week_order,
        "daily":           daily,
    }
