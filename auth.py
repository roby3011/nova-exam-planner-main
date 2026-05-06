"""Small auth helpers for the local Streamlit app."""

import hashlib
import hmac
import re
import secrets
from typing import Optional

import streamlit as st

import database as db

_PBKDF2_ITERS = 150_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERS
    ).hex()
    return f"{salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERS
    ).hex()
    return hmac.compare_digest(got, expected)


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_username(u: str) -> bool:
    return bool(_USERNAME_RE.match(u or ""))


def is_valid_email(e: str) -> bool:
    return not e or bool(_EMAIL_RE.match(e))


def is_valid_password(p: str) -> tuple[bool, str]:
    if len(p) < 8:
        return False, "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", p):
        return False, "Password must contain a letter."
    if not re.search(r"\d", p):
        return False, "Password must contain a digit."
    return True, ""


def register_user(username: str, email: str, password: str,
                  display_name: Optional[str] = None,
                  country_code: str = db.DEFAULT_COUNTRY):
    """Return (user_id, error)."""
    if not is_valid_username(username):
        return None, "Username must be 3-20 characters (letters, digits, _)."
    if not is_valid_email(email):
        return None, "Please enter a valid email address."
    ok, msg = is_valid_password(password)
    if not ok:
        return None, msg
    if db.get_user_by_username(username):
        return None, "That username is already taken."
    user_id = db.create_user(
        username=username.strip(),
        email=email.strip() or None,
        password_hash=hash_password(password),
        display_name=(display_name or username).strip(),
        country_code=country_code,
    )
    return user_id, None


def authenticate(username: str, password: str):
    user = db.get_user_by_username(username)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def login_session(user: dict):
    st.session_state["user_id"] = user["id"]
    st.session_state["username"] = user["username"]


def logout():
    for k in list(st.session_state.keys()):
        del st.session_state[k]


def current_user() -> Optional[dict]:
    """Return the logged-in user, if any."""
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    u = db.get_user_by_id(uid)
    if not u:
        logout()
        return None
    return u


def change_password(user_id: int, current_pw: str, new_pw: str) -> tuple[bool, str]:
    u = db.get_user_by_id(user_id)
    if not u:
        return False, "User not found."
    if not verify_password(current_pw, u["password_hash"]):
        return False, "Current password is incorrect."
    ok, msg = is_valid_password(new_pw)
    if not ok:
        return False, msg
    db.update_user_password(user_id, hash_password(new_pw))
    return True, "Password updated."
