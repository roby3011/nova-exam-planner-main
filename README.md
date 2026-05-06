# Nova Exam Planner

Nova Exam Planner is a Streamlit app for planning study sessions around exam dates.
It supports multiple users, stores data in SQLite, can skip public holidays, and
exports the generated plan as CSV or calendar events. It also shows the Nova SBE
cafeteria menu for the week.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

The first run creates `data/nova.db` automatically.

## Main Files

```text
app.py            Streamlit pages and UI
auth.py           Registration, login, password hashing
database.py       SQLite tables and CRUD helpers
scheduler.py      Study-plan generation and analytics helpers
api_client.py     Public holidays, country-list, and cafeteria API helpers
requirements.txt  Python dependencies
```

## Data Model

- `users`: account and profile details
- `courses`: exam date, ECTS, difficulty, and estimated study hours
- `constraints`: weekly hours, daily limit, available days, and holiday setting
- `study_sessions`: planned and completed minutes per course/date

All course, constraint, and session rows are scoped by `user_id`.

## Features

- Register and log in with local accounts
- Add courses and study constraints
- Generate a balanced study plan before each exam
- Track completed sessions and redistribute missed study time
- Review weekly calendar, progress charts, and per-course totals
- Check the current Nova SBE cafeteria weekly menu
- Use a simple focus timer and log study minutes
- Import/export courses and export the plan as CSV or ICS
