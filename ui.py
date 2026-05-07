"""Small UI helpers shared by the Streamlit pages."""

from html import escape
import streamlit as st


CUSTOM_CSS = """
<style>
:root {
    --paper: #f7f6f1;
    --panel: #ffffff;
    --ink: #111111;
    --muted: #666666;
    --line: rgba(17, 17, 17, 0.2);
    --line-strong: rgba(17, 17, 17, 0.28);
    --accent: #2563eb;
    --success: #16a34a;
    --warn: #d97706;
    --danger: #dc2626;
    --soft: #f1f1ef;
    --soft-2: #e8e8e6;
    --radius-lg: 26px;
    --radius-md: 18px;
    --shadow: 0 20px 50px rgba(17, 17, 17, 0.08);
}

.stApp {
    background: var(--paper);
    color: var(--ink);
}

/* Mobile: keep header visible — it holds the sidebar toggle button */
header[data-testid="stHeader"] {
    background: var(--paper);
}

/* Desktop: hide the header bar entirely to remove the dead space above content */
@media screen and (min-width: 768px) {
    header[data-testid="stHeader"] {
        display: none !important;
    }
    section.main,
    section[data-testid="stMain"] {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
}

section.main,
section[data-testid="stMain"],
[data-testid="stAppViewContainer"] {
    scroll-padding-top: 0.75rem !important;
}

.main .block-container,
[data-testid="stMain"] .block-container {
    max-width: 1180px;
    padding-top: 0.75rem !important;
    padding-bottom: 3rem;
}

h1, h2, h3 {
    color: var(--ink);
    letter-spacing: 0;
}

p, li, label, span {
    letter-spacing: 0;
}

[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] p,
.stTextInput label,
.stNumberInput label,
.stDateInput label {
    color: var(--ink) !important;
    opacity: 1 !important;
}

[data-testid="stSidebar"] {
    background: var(--paper);
    border-right: 1px solid var(--line);
}

div[data-testid="stMetric"] {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    padding: 1rem 1.1rem;
    box-shadow: 0 10px 25px rgba(17, 17, 17, 0.04);
    min-height: 120px;
}

code {
    background: var(--soft) !important;
    border: 1px solid var(--line);
    border-radius: 10px !important;
    color: var(--ink) !important;
    font-family: inherit !important;
    font-weight: 700;
}

[data-testid="stAlert"] {
    background: var(--soft-2) !important;
    border: 1.5px solid var(--line) !important;
    border-radius: 14px !important;
    color: var(--ink) !important;
    box-shadow: none !important;
}

[data-testid="stAlertContainer"],
div[role="alert"] {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
}

[data-testid="stAlert"] *,
[data-testid="stAlertContainer"] *,
div[role="alert"] * {
    color: var(--ink) !important;
}

[data-testid="stAlert"] svg,
[data-testid="stAlertContainer"] svg,
div[role="alert"] svg {
    display: none;
}

/* ── Reset ALL inner elements — no border ever on the raw input element ── */
[data-baseweb="input"] input,
[data-baseweb="input"] textarea,
[data-baseweb="base-input"],
[data-baseweb="base-input"] input,
[data-baseweb="base-input"] textarea {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    color: var(--ink) !important;
    box-shadow: none !important;
}

/* ── Text and date inputs: baseweb input IS the full container ── */
[data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stDateInput"] [data-baseweb="input"] {
    background: #ffffff !important;
    border: 1.5px solid rgba(17,17,17,0.2) !important;
    border-radius: 14px !important;
    overflow: hidden;
}
[data-testid="stTextInput"] [data-baseweb="input"]:focus-within,
[data-testid="stDateInput"] [data-baseweb="input"]:focus-within {
    border-color: rgba(17,17,17,0.32) !important;
    box-shadow: 0 0 0 3px rgba(17,17,17,0.08) !important;
    outline: none !important;
}

/* ── Number inputs: CSS best-effort via :has() (direct-child variant) ──
   JS in apply_theme() is the reliable fallback. */
[data-testid="stNumberInput"] div:has(> [data-baseweb="input"]) {
    background: #ffffff !important;
    border: 1.5px solid rgba(17,17,17,0.2) !important;
    border-radius: 14px !important;
    overflow: hidden;
}
[data-testid="stNumberInput"] div:has(> [data-baseweb="input"]) > [data-baseweb="input"] {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
}
[data-testid="stNumberInput"] div:has(> [data-baseweb="input"]):focus-within {
    border-color: rgba(17,17,17,0.32) !important;
    box-shadow: 0 0 0 3px rgba(17,17,17,0.08) !important;
}

/* Number input ± step buttons */
[data-testid="stNumberInput"] button {
    background: transparent !important;
    border: none !important;
    color: var(--ink) !important;
}

/* ── Select boxes ── */
[data-baseweb="select"] {
    background: #ffffff !important;
    border: 1.5px solid rgba(17,17,17,0.2) !important;
    border-radius: 14px !important;
    overflow: hidden;
}
[data-baseweb="select"] > div {
    border: none !important;
    background: transparent !important;
}
[data-baseweb="select"]:focus-within {
    border-color: rgba(17,17,17,0.32) !important;
    box-shadow: 0 0 0 3px rgba(17,17,17,0.08) !important;
}

/* ── Textareas ── */
[data-baseweb="textarea"] {
    background: #ffffff !important;
    border: 1.5px solid rgba(17,17,17,0.2) !important;
    border-radius: 14px !important;
    overflow: hidden;
}
[data-baseweb="textarea"]:focus-within {
    border-color: rgba(17,17,17,0.32) !important;
    box-shadow: 0 0 0 3px rgba(17,17,17,0.08) !important;
}

input::placeholder,
textarea::placeholder {
    color: #7a7a7a !important;
    opacity: 1 !important;
}

[data-baseweb="tab-highlight"] {
    background-color: var(--ink) !important;
}

/* Secondary / default buttons */
[data-testid="stBaseButton-secondary"] button,
[data-testid="stBaseButton-secondaryFormSubmit"] button,
[data-testid="stFormSubmitButton"] button,
button[kind="secondary"],
button[kind="secondaryFormSubmit"] {
    border: 1.5px solid var(--line) !important;
    border-radius: 14px !important;
    background: var(--panel) !important;
    color: var(--ink) !important;
    font-weight: 700 !important;
    box-shadow: 0 8px 18px rgba(17, 17, 17, 0.05) !important;
}

[data-testid="stBaseButton-secondary"] button:hover,
[data-testid="stBaseButton-secondaryFormSubmit"] button:hover {
    background: var(--soft) !important;
}

/* Primary buttons */
[data-testid="stBaseButton-primary"] button,
[data-testid="stBaseButton-primaryFormSubmit"] button,
button[kind="primary"],
button[kind="primaryFormSubmit"] {
    background: var(--ink) !important;
    color: #ffffff !important;
    border: 1.5px solid var(--ink) !important;
    border-radius: 14px !important;
    font-weight: 700 !important;
}

[data-testid="stBaseButton-primary"] button *,
[data-testid="stBaseButton-primaryFormSubmit"] button *,
button[kind="primary"] *,
button[kind="primaryFormSubmit"] * {
    color: #ffffff !important;
}

[data-testid="stBaseButton-primary"] button:hover,
[data-testid="stBaseButton-primaryFormSubmit"] button:hover {
    opacity: 0.88 !important;
}

.stProgress > div > div {
    background: #dde3f0 !important;
    border-radius: 999px !important;
    overflow: hidden !important;
}

.stProgress > div > div > div > div {
    background: var(--accent) !important;
    border-radius: 999px !important;
}

.stTabs [data-baseweb="tab"] {
    border: 1px solid var(--line);
    border-bottom: none;
    border-radius: 14px 14px 0 0;
    background: var(--paper);
    padding: 0.55rem 1rem;
}

.stTabs [role="tab"] p {
    color: var(--ink) !important;
    opacity: 1 !important;
}

.stTabs [aria-selected="true"] {
    background: var(--panel);
    color: var(--ink);
}

.nova-page-title {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow);
    padding: 1.5rem 1.6rem 1.65rem;
    margin-bottom: 1.2rem;
}

.nova-title-row {
    display: flex;
    gap: 1rem;
    align-items: center;
}

.nova-title-logo {
    flex: 0 0 auto;
    width: 96px;
    min-height: 62px;
    border-radius: 18px;
    background: var(--ink);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.7rem;
}

.nova-title-logo img {
    display: block;
    width: 100%;
    height: auto;
}

.nova-kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.78rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 0.35rem;
}

.nova-page-title h1 {
    margin: 0;
    font-size: clamp(2rem, 4vw, 3.25rem);
    line-height: 0.95;
    font-weight: 900;
}

.nova-page-title p {
    margin: 0.35rem 0 0;
    color: var(--muted);
}

.nova-sidebar-logo {
    background: var(--ink);
    border: 1px solid var(--ink);
    border-radius: 18px;
    padding: 0.9rem 0.85rem;
    margin: 0.25rem 0 1.1rem;
    box-shadow: 0 14px 34px rgba(17, 17, 17, 0.1);
}

.nova-sidebar-logo img {
    display: block;
    width: min(132px, 100%);
    height: auto;
}

.nova-sidebar-meta {
    border-bottom: 1px solid var(--line);
    padding-bottom: 1rem;
    margin-bottom: 1rem;
}

.nova-sidebar-meta .hello {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
}

.nova-sidebar-meta .name {
    font-weight: 800;
    color: var(--ink);
}

[data-testid="stSidebar"] [data-testid="stRadio"] label {
    min-height: 2.35rem;
    border-bottom: 1px solid var(--line);
    padding: 0.3rem 0;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label > div:first-child {
    display: none !important;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label p {
    color: var(--ink) !important;
    font-size: 1rem;
    font-weight: 700;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) p {
    font-weight: 900;
    text-decoration: underline;
    text-decoration-thickness: 2px;
    text-underline-offset: 0.25rem;
}

.nova-task {
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: 16px;
    padding: 0.7rem 0.85rem;
    margin-bottom: 0.45rem;
    background: var(--panel);
}

.nova-task.done {
    border-left-color: var(--success);
    background: #f0f0f0;
}

.nova-task .t-title {
    font-weight: 800;
}

.nova-task .t-meta {
    font-size: 0.85rem;
    color: var(--muted);
}

.nova-chip {
    display: inline-block;
    padding: 0.2rem 0.55rem;
    border: 1px solid currentColor;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 800;
    margin-right: 0.45rem;
}

.chip-urgent { color: var(--ink); background: #eeeeee; }
.chip-warn { color: var(--ink); background: #f4f4f4; }
.chip-ok { color: var(--ink); background: #ffffff; }

.calendar-day {
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--panel);
    padding: 0.5rem 0.25rem;
    text-align: center;
    margin-bottom: 0.5rem;
}

.calendar-day.today {
    background: var(--ink);
    color: #ffffff;
}

.mini-session {
    border-left: 3px solid var(--accent);
    background: #f8f8f8;
    border-radius: 12px;
    padding: 0.25rem 0.45rem;
    margin: 0.25rem 0;
    font-size: 0.82rem;
}

.exam-tag {
    border: 1px solid var(--danger);
    color: var(--danger);
    border-radius: 999px;
    padding: 0.2rem 0.4rem;
    font-size: 0.76rem;
    text-align: center;
    margin-top: 0.4rem;
    font-weight: 800;
}

.nova-lunch-line {
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    color: var(--ink);
    font-size: 1.05rem;
    line-height: 1.45;
    padding: 0.9rem 1.05rem;
    margin-bottom: 0.45rem;
}

.nova-weekly-menu {
    display: grid;
    gap: 0.75rem;
    grid-template-columns: repeat(auto-fit, minmax(245px, 1fr));
    margin-top: 0.75rem;
}

.nova-menu-day {
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    padding: 0.8rem 0.9rem 0.7rem;
}

.nova-menu-day h4 {
    color: var(--ink);
    font-size: 0.98rem;
    line-height: 1.2;
    margin: 0 0 0.55rem;
}

.nova-menu-row {
    border-top: 1px solid var(--line);
    padding: 0.45rem 0 0.05rem;
}

.nova-menu-row:first-of-type {
    border-top: 0;
    padding-top: 0;
}

.nova-menu-row span {
    color: var(--muted);
    display: block;
    font-size: 0.72rem;
    font-weight: 800;
    margin-bottom: 0.08rem;
    text-transform: uppercase;
}

.nova-menu-row p {
    color: var(--ink);
    font-size: 0.9rem;
    line-height: 1.35;
    margin: 0;
}

[class*="st-key-quickstart_actions"] {
    margin-top: 0.9rem;
}

[class*="st-key-quickstart_actions"] [data-testid="stHorizontalBlock"] {
    gap: 1rem;
}

[class*="st-key-quickstart_actions"] [data-testid="column"] {
    min-width: 0;
}

[class*="st-key-quickstart_actions"] [data-testid="stButton"] button {
    min-height: 154px;
    display: flex !important;
    align-items: flex-start !important;
    justify-content: flex-start !important;
    text-align: left !important;
    white-space: normal !important;
    padding: 1.3rem 1.45rem;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: rgba(255, 255, 255, 0.76);
    box-shadow: 0 12px 32px rgba(17, 17, 17, 0.04);
    cursor: pointer;
    transition: transform 140ms ease, box-shadow 140ms ease,
        background 140ms ease;
}

[class*="st-key-quickstart_actions"] [data-testid="stButton"] button:hover {
    background: #ffffff;
    transform: translateY(-2px);
    box-shadow: 0 20px 46px rgba(17, 17, 17, 0.08);
}

[class*="st-key-quickstart_actions"] [data-testid="stButton"] button p {
    color: var(--muted) !important;
    font-size: 0.98rem;
    line-height: 1.45;
    font-weight: 600;
    white-space: normal !important;
    text-align: left !important;
}

[class*="st-key-quickstart_actions"] [data-testid="stButton"] button p:first-child {
    margin-bottom: 0.75rem;
}

[class*="st-key-quickstart_actions"] [data-testid="stButton"] button strong {
    color: var(--ink) !important;
    font-size: clamp(1.3rem, 2vw, 1.75rem);
    line-height: 1.05;
    font-weight: 900;
}

/* Expander */
[data-testid="stExpander"] details {
    background: var(--panel) !important;
    border: 1.5px solid var(--line) !important;
    border-radius: 14px !important;
    overflow: hidden;
}

[data-testid="stExpander"] details summary,
[data-testid="stExpander"] details summary * {
    background: var(--panel) !important;
    color: var(--ink) !important;
}

[data-testid="stExpander"] details > div {
    background: var(--panel) !important;
    color: var(--ink) !important;
}

[data-testid="stExpander"] details > div * {
    color: var(--ink) !important;
}

/* ── Course cards ─────────────────────────────────── */

.course-card {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
}

.course-card-name {
    font-size: 0.93rem;
    font-weight: 700;
    color: var(--ink);
    line-height: 1.3;
    margin: 0;
    padding-top: 0.05rem;
}

.course-meta {
    font-size: 0.74rem;
    color: var(--muted);
    line-height: 1.4;
    margin: 0;
    padding: 0;
}

.course-meta-sep {
    display: inline-block;
    margin: 0 0.3rem;
    opacity: 0.3;
}

.course-exam-row {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin-top: 0.1rem;
}

.course-exam-date {
    font-size: 0.74rem;
    font-weight: 600;
    color: var(--ink);
}

.course-status-badge {
    display: inline-block;
    font-size: 0.64rem;
    font-weight: 700;
    padding: 0.1rem 0.42rem;
    border-radius: 4px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    vertical-align: middle;
}

.course-status-past   { background: var(--soft-2); color: var(--muted); }
.course-status-urgent { background: #fef3c7; color: #92400e; }
.course-status-soon   { background: #dbeafe; color: #1d4ed8; }
.course-status-normal { background: var(--soft); color: var(--muted); }

/* Course card action buttons */
.nova-btn-edit button {
    background: transparent !important;
    color: var(--muted) !important;
    border: 1px solid var(--line) !important;
    border-radius: 8px !important;
    font-size: 0.74rem !important;
    font-weight: 600 !important;
    padding: 0 0.6rem !important;
    height: 28px !important;
    min-height: unset !important;
}

.nova-btn-edit button:hover {
    background: var(--soft) !important;
    color: var(--ink) !important;
    border-color: var(--line-strong) !important;
}

.nova-btn-delete button {
    background: transparent !important;
    color: var(--muted) !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    font-size: 0.82rem !important;
    font-weight: 400 !important;
    padding: 0 0.45rem !important;
    height: 28px !important;
    min-height: unset !important;
}

.nova-btn-delete button:hover {
    background: #fff1f2 !important;
    color: var(--danger) !important;
    border-color: rgba(220, 38, 38, 0.18) !important;
}

/* Motivational quote card */
.nova-quote {
    background: var(--panel);
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: var(--radius-md);
    padding: 0.85rem 1rem;
    margin-bottom: 1rem;
    color: var(--ink);
}

.nova-quote .q-text {
    font-size: 1rem;
    font-style: italic;
    line-height: 1.5;
    color: var(--ink);
}

.nova-quote .q-author {
    font-size: 0.82rem;
    font-weight: 700;
    color: var(--muted);
    margin-top: 0.35rem;
}

/* Estimated hours live display */
.nova-est-hours {
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: var(--radius-md);
    padding: 0.75rem 1rem;
    margin-bottom: 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
}

.nova-est-hours .est-label {
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #3b82f6;
}

.nova-est-hours .est-value {
    font-size: 1.4rem;
    font-weight: 900;
    color: #1d4ed8;
}

.nova-est-hours .est-formula {
    font-size: 0.82rem;
    color: #6b7280;
}


@media (max-width: 900px) {
    .nova-title-row {
        flex-direction: column;
        align-items: flex-start;
    }

    [class*="st-key-quickstart_actions"] [data-testid="stHorizontalBlock"] {
        flex-direction: column;
    }
}

/* Pills — uniform 1.5px border on all states */
[data-testid="stPills"] button {
    border: 1.5px solid rgba(17,17,17,0.35) !important;
    border-radius: 999px !important;
    background: #ffffff !important;
    color: var(--ink) !important;
    font-weight: 500 !important;
    font-size: 0.88rem !important;
    padding: 4px 16px !important;
    transition: background 0.15s, color 0.15s, border-color 0.15s !important;
    box-shadow: none !important;
}
[data-testid="stPills"] button:hover {
    border-color: var(--ink) !important;
    background: var(--soft) !important;
}
[data-testid="stPills"] button[aria-selected="true"],
[data-testid="stPills"] button[data-selected="true"],
[data-testid="stPills"] button[kind="pillsActive"],
[data-testid="stPills"] [aria-pressed="true"] {
    background: var(--ink) !important;
    color: #ffffff !important;
    border-color: var(--ink) !important;
}

</style>
"""


_NUMBER_INPUT_JS = """
<script>
(function(){
  var BORDER = '1.5px solid rgba(17,17,17,0.2)';
  var RADIUS = '14px';
  function fix(){
    try {
      var doc = window.parent.document;
      doc.querySelectorAll('[data-testid="stNumberInput"]').forEach(function(widget){
        var bwInput = widget.querySelector('[data-baseweb="input"]');
        if (!bwInput) return;
        var container = bwInput.parentElement;
        // Walk up if the immediate parent is also a baseweb element
        if (!container || container === widget) return;
        if (container.style.border === BORDER) return; // already done
        container.style.cssText += [
          'border:' + BORDER,
          'border-radius:' + RADIUS,
          'overflow:hidden',
          'background:#ffffff'
        ].join('!important;') + '!important;';
        bwInput.style.cssText += 'border:none!important;background:transparent!important;border-radius:0!important;';
      });
    } catch(e){}
  }
  fix();
  try {
    new MutationObserver(fix)
      .observe(window.parent.document.body, {childList:true, subtree:true});
  } catch(e){}
})();
</script>
"""


def apply_theme():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.components.v1.html(_NUMBER_INPUT_JS, height=0)


def fmt_minutes(minutes: float) -> str:
    minutes = max(0, round(minutes))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_hours(hours: float) -> str:
    return fmt_minutes(hours * 60)


def h(value) -> str:
    """Escape text before placing it inside custom HTML."""
    return escape(str(value), quote=True)


def scroll_to_top():
    st.html(
        "<script>window.parent.document.querySelector('section.main')"
        ".scrollTo(0,0);</script>"
    )


def render_page_title(title: str, subtitle: str = "", kicker: str = "nova",
                      logo_src: str = ""):
    scroll_to_top()
    subtitle_html = f"<p>{h(subtitle)}</p>" if subtitle else ""
    logo_html = (
        f'<div class="nova-title-logo"><img src="{h(logo_src)}" alt="Nova SBE" /></div>'
        if logo_src else ""
    )
    html = (
        '<div class="nova-page-title">'
        '<div class="nova-title-row">'
        f'{logo_html}'
        '<div>'
        f'<div class="nova-kicker">{h(kicker)}</div>'
        f'<h1>{h(title)}</h1>'
        f'{subtitle_html}'
        '</div>'
        '</div>'
        '</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def render_task_tile(course_name: str, planned_minutes: int,
                     completed_minutes: int = 0, color: str = "#111111"):
    done = completed_minutes >= planned_minutes > 0
    klass = "nova-task done" if done else "nova-task"
    status = "Done" if done else "Planned"
    completed = (
        f" / {fmt_minutes(completed_minutes)} completed"
        if completed_minutes else ""
    )
    html = (
        f'<div class="{klass}" style="border-left-color:{h(color)};">'
        f'<div class="t-title">{h(course_name)}</div>'
        f'<div class="t-meta">{status}: {fmt_minutes(planned_minutes)}{completed}</div>'
        '</div>'
    )
    st.markdown(html, unsafe_allow_html=True)
