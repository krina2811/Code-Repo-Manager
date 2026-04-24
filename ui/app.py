"""
Code Repository Manager — Dark Mode UI

Manager dashboard with:
- Login / Register
- Project registration & continuous file watching
- Real-time notifications when reviews are pending
- Approve / Reject queue
- Dashboard stats
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import streamlit as st
import streamlit.components.v1 as components
import requests
import time
from datetime import datetime

st.set_page_config(
    page_title="Code Review Manager",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── Dark mode CSS ────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* ── Base dark theme ── */
  .stApp { background: #0f1117; color: #e2e8f0; }
  #MainMenu, footer, header { visibility: hidden; }

  /* ── Sidebar ── */
  [data-testid="stSidebar"] { background: #1a1d27; border-right: 1px solid #2d3748; }

  /* ── Tabs ── */
  .stTabs [data-baseweb="tab-list"] {
    background: #1a1d27;
    border-radius: 10px;
    padding: 4px;
    gap: 4px;
  }
  .stTabs [data-baseweb="tab"] {
    background: transparent;
    color: #94a3b8;
    border-radius: 8px;
    font-weight: 600;
    padding: 8px 20px;
  }
  .stTabs [aria-selected="true"] {
    background: #6366f1 !important;
    color: white !important;
  }

  /* ── Inputs & textareas ── */
  .stTextInput > div > div > input,
  .stTextArea > div > div > textarea,
  .stSelectbox > div > div {
    background: #1e2130 !important;
    color: #e2e8f0 !important;
    border: 1px solid #2d3748 !important;
    border-radius: 8px !important;
  }
  .stTextArea > div > div > textarea::placeholder,
  .stTextInput > div > div > input::placeholder {
    color: #64748b !important;
    opacity: 1 !important;
  }
  /* label above text areas/inputs */
  .stTextArea label, .stTextInput label {
    color: #94a3b8 !important;
  }

  /* ── Buttons — base (secondary / default) ── */
  .stButton > button {
    background: #252a3a !important;
    color: #e2e8f0 !important;
    border: 1px solid #3d4a63 !important;
    border-radius: 8px !important;
    font-weight: 600;
    transition: all 0.2s;
  }
  .stButton > button:hover {
    background: #2e3549 !important;
    border-color: #6366f1 !important;
    color: #ffffff !important;
  }
  .stButton > button:disabled {
    background: #1a1d27 !important;
    color: #475569 !important;
    border-color: #2d3748 !important;
    cursor: not-allowed;
  }

  /* ── Buttons — primary (Approve / Login / etc.) ── */
  .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
    border: none !important;
    color: white !important;
  }
  .stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #4f46e5, #7c3aed) !important;
    transform: translateY(-1px);
    box-shadow: 0 4px 15px rgba(99,102,241,0.4);
  }

  /* ── Button wrappers: strip extra spacing so columns stay aligned ── */
  .btn-danger, .btn-neutral {
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1 !important;
  }
  .btn-danger .stButton, .btn-neutral .stButton {
    margin: 0 !important;
  }

  /* ── Reject button: second column next to a primary Approve button ── */
  /* Targets the secondary button that sits beside a primary button in a 2-col row */
  [data-testid="column"] + [data-testid="column"] .stButton > button:not([kind="primary"]) {
    background: #2d0a0a !important;
    color: #f87171 !important;
    border-color: #7f1d1d !important;
  }
  [data-testid="column"] + [data-testid="column"] .stButton > button:not([kind="primary"]):hover {
    background: #450a0a !important;
    border-color: #ef4444 !important;
    color: #fca5a5 !important;
  }

  /* ── .btn-danger wrapper (used for Remove project button) ── */
  .btn-danger .stButton > button {
    background: #2d0a0a !important;
    color: #f87171 !important;
    border-color: #7f1d1d !important;
  }
  .btn-danger .stButton > button:hover {
    background: #450a0a !important;
    border-color: #ef4444 !important;
    color: #fca5a5 !important;
  }

  /* ── Neutral action button wrapper (Refresh, Re-analyze, Mark read, etc.) ── */
  .btn-neutral .stButton > button {
    background: #1e2130 !important;
    color: #94a3b8 !important;
    border-color: #2d3748 !important;
  }
  .btn-neutral .stButton > button:hover {
    background: #252a3a !important;
    color: #e2e8f0 !important;
    border-color: #475569 !important;
  }

  /* ── Expanders ── */
  .streamlit-expanderHeader {
    background: #1a1d27 !important;
    border: 1px solid #2d3748 !important;
    border-radius: 8px !important;
    color: #e2e8f0 !important;
  }
  .streamlit-expanderContent {
    background: #161925 !important;
    border: 1px solid #2d3748 !important;
    border-top: none !important;
    border-radius: 0 0 8px 8px !important;
  }

  /* ── Metrics ── */
  [data-testid="stMetric"] {
    background: #1a1d27;
    border: 1px solid #2d3748;
    border-radius: 10px;
    padding: 1rem 1.2rem;
  }
  [data-testid="stMetricLabel"] { color: #94a3b8 !important; font-size: 0.8rem; }
  [data-testid="stMetricValue"] { color: #e2e8f0 !important; }

  /* ── Info / Success / Warning / Error boxes ── */
  .stAlert { border-radius: 8px !important; }
  [data-baseweb="notification"][kind="info"]    { background: #1e3a5f !important; }
  [data-baseweb="notification"][kind="success"] { background: #14532d !important; }
  [data-baseweb="notification"][kind="warning"] { background: #451a03 !important; }
  [data-baseweb="notification"][kind="error"]   { background: #450a0a !important; }

  /* ── Custom cards ── */
  .glass-card {
    background: #1a1d27;
    border: 1px solid #2d3748;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1rem;
  }
  .project-card {
    background: linear-gradient(135deg, #1e2130, #1a1d27);
    border: 1px solid #2d3748;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 0.75rem;
    transition: border-color 0.2s;
  }
  .project-card:hover { border-color: #6366f1; }

  /* ── Auth card ── */
  .auth-card {
    background: #1a1d27;
    border: 1px solid #2d3748;
    border-radius: 16px;
    padding: 2rem 2.5rem;
    max-width: 420px;
    margin: 2rem auto;
  }

  /* ── Notification badge ── */
  .notif-badge {
    background: #ef4444;
    color: white;
    border-radius: 50%;
    padding: 2px 7px;
    font-size: 0.75rem;
    font-weight: 700;
    margin-left: 6px;
    vertical-align: middle;
  }

  /* ── Status dots ── */
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .dot-watching  { background:#22c55e; box-shadow:0 0 6px #22c55e; }
  .dot-analyzing { background:#f59e0b; box-shadow:0 0 6px #f59e0b; animation: pulse 1s infinite; }
  .dot-error     { background:#ef4444; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  /* ── Risk badges ── */
  .badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.78rem; font-weight:700; }
  .badge-low      { background:#052e16; color:#4ade80; border:1px solid #166534; }
  .badge-medium   { background:#2d1b00; color:#fbbf24; border:1px solid #92400e; }
  .badge-high     { background:#2d0a0a; color:#f87171; border:1px solid #991b1b; }
  .badge-critical { background:#1a0020; color:#e879f9; border:1px solid #7e22ce; }

  /* ── Hero ── */
  .hero-title {
    font-size: 2.6rem;
    font-weight: 800;
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a78bfa 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    line-height: 1.2;
  }
  .hero-sub { color: #64748b; font-size: 1.05rem; margin-top: 0.4rem; }

  /* ── Divider ── */
  hr { border-color: #2d3748 !important; margin: 1.2rem 0 !important; }

  /* ── Spinner ── */
  .stSpinner > div { border-top-color: #6366f1 !important; }
</style>
""", unsafe_allow_html=True)

import os as _os
API_BASE = _os.getenv("API_BASE_URL", "http://localhost:8000")

# ─── Session state ─────────────────────────────────────────────────────────────
for key, default in [
    ("token",    None),
    ("username", None),
    ("role",     None),
    ("notif_count", 0),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ─── Auth helpers ──────────────────────────────────────────────────────────────

def _auth_headers() -> dict:
    token = st.session_state.get("token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def is_authenticated() -> bool:
    return bool(st.session_state.get("token"))


def logout():
    st.session_state.token = None
    st.session_state.username = None
    st.session_state.role = None
    st.rerun()


# ─── API helpers ───────────────────────────────────────────────────────────────

def _get(path, **kw):
    try:
        r = requests.get(f"{API_BASE}{path}", headers=_auth_headers(), timeout=5, **kw)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            st.session_state.token = None
            st.rerun()
        return None
    except Exception:
        return None


def _post(path, json=None, params=None, timeout=30):
    try:
        r = requests.post(
            f"{API_BASE}{path}",
            json=json,
            params=params,
            headers=_auth_headers(),
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            st.session_state.token = None
            st.rerun()
        try:
            return {"error": e.response.json().get("detail", str(e))}
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def _delete(path):
    try:
        r = requests.delete(f"{API_BASE}{path}", headers=_auth_headers(), timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            st.session_state.token = None
            st.rerun()
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def _post_public(path, json=None):
    """POST without auth header — for login/register."""
    try:
        r = requests.post(f"{API_BASE}{path}", json=json, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        try:
            return {"error": e.response.json().get("detail", str(e))}
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def api_ok():
    try:
        return requests.get(f"{API_BASE}/", timeout=2).status_code == 200
    except Exception:
        return False


# ─── Notification helper ────────────────────────────────────────────────────

def fetch_notifications():
    return _get("/api/notifications") or {"notifications": [], "unread_count": 0}


# ─── Risk badge HTML ────────────────────────────────────────────────────────

def risk_badge(level: str) -> str:
    icons = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "💀"}
    return (
        f"<span class='badge badge-{level.lower()}'>"
        f"{icons.get(level.lower(), '⚪')} {level.upper()}"
        f"</span>"
    )


def status_dot(status: str) -> str:
    cls = {"watching": "dot-watching", "analyzing": "dot-analyzing"}.get(status, "dot-error")
    return f"<span class='dot {cls}'></span>"


# ─── Auth page ─────────────────────────────────────────────────────────────────

def show_auth_page():
    st.markdown(
        "<div style='text-align:center;padding-top:2rem'>"
        "<div class='hero-title'>Code Review Manager</div>"
        "<div class='hero-sub'>Continuous analysis · Human-in-the-loop approval</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    # Disable browser password-manager popup on all password inputs.
    # st.markdown does NOT execute <script> tags; components.html() runs in an
    # iframe that can reach window.parent.document (same localhost origin).
    # Disable browser password-manager popup.
    # components.html() executes JS in an iframe; window.parent.document reaches
    # the main Streamlit page (same localhost origin).
    # autocomplete="one-time-code" + randomised name prevents Chrome from
    # classifying the field as a credentials input and showing its popup.
    components.html("""
    <script>
    (function () {
      var _patched = new WeakSet();
      function patch() {
        var doc = window.parent.document;
        doc.querySelectorAll('input[type="password"]').forEach(function (el) {
          if (_patched.has(el)) return;
          el.setAttribute('autocomplete', 'one-time-code');
          el.setAttribute('name', 'f_' + Math.random().toString(36).slice(2));
          _patched.add(el);
        });
      }
      new MutationObserver(patch).observe(
        window.parent.document.body, { childList: true, subtree: true }
      );
      setInterval(patch, 300);
      patch();
    })();
    </script>
    """, height=0)

    col_l, col_m, col_r = st.columns([1, 2, 1])
    with col_m:
        login_tab, register_tab = st.tabs(["🔑 Login", "📝 Register"])

        with login_tab:
            login_email = st.text_input("Email", key="login_email", placeholder="you@example.com")
            login_pass  = st.text_input("Password", type="password", key="login_pass")
            if st.button("Login", type="primary", use_container_width=True, key="btn_login"):
                if not login_email or not login_pass:
                    st.error("Email and password are required.")
                else:
                    result = _post_public("/api/auth/login", json={"email": login_email, "password": login_pass})
                    if result and "error" not in result:
                        st.session_state.token    = result["access_token"]
                        st.session_state.username = result["username"]
                        st.session_state.role     = result.get("role", "manager")
                        st.rerun()
                    else:
                        st.error(result.get("error", "Login failed") if result else "Login failed")

        with register_tab:
            reg_email = st.text_input("Email", key="reg_email", placeholder="you@example.com")
            reg_pass  = st.text_input("Password", type="password", key="reg_pass",
                                      help="Min 8 characters, at least one letter and one number")
            reg_pass2 = st.text_input("Confirm Password", type="password", key="reg_pass2")

            # Client-side validation feedback
            if reg_pass:
                strength_ok = len(reg_pass) >= 8 and any(c.isdigit() for c in reg_pass) and any(c.isalpha() for c in reg_pass)
                if not strength_ok:
                    st.warning("Password must be 8+ characters with at least one letter and one number.")

            if st.button("Create Account", type="primary", use_container_width=True, key="btn_reg"):
                import re as _re
                if not reg_email or not reg_pass or not reg_pass2:
                    st.error("All fields are required.")
                elif not _re.match(r'^[^@]+@[^@]+\.[^@]+$', reg_email):
                    st.error("Enter a valid email address.")
                elif len(reg_pass) < 8:
                    st.error("Password must be at least 8 characters.")
                elif not any(c.isdigit() for c in reg_pass) or not any(c.isalpha() for c in reg_pass):
                    st.error("Password must contain at least one letter and one number.")
                elif reg_pass != reg_pass2:
                    st.error("Passwords do not match.")
                else:
                    result = _post_public("/api/auth/register", json={
                        "email": reg_email, "password": reg_pass, "confirm_password": reg_pass2,
                    })
                    if result and "error" not in result:
                        st.session_state.token    = result["access_token"]
                        st.session_state.username = result["username"]
                        st.session_state.role     = result.get("role", "manager")
                        st.rerun()
                    else:
                        st.error(result.get("error", "Registration failed") if result else "Registration failed")


# ─── Header ────────────────────────────────────────────────────────────────────

def show_header(unread: int = 0):
    badge = f"<span class='notif-badge'>{unread}</span>" if unread else ""

    col_brand, col_user = st.columns([5, 2])
    with col_brand:
        st.markdown(
            "<div style='display:flex;align-items:center;gap:0.5rem'>"
            "<span style='font-size:2rem;line-height:1'>🛡️</span>"
            "<div>"
            "<div class='hero-title' style='line-height:1.1'>Code Review Manager</div>"
            "<div class='hero-sub'>Continuous analysis · Human-in-the-loop approval</div>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with col_user:
        st.markdown(
            f"<div style='text-align:right;padding-top:12px;font-size:1.1rem'>"
            f"🔔 Notifications {badge}</div>",
            unsafe_allow_html=True,
        )
        username = st.session_state.get("username", "")
        role     = st.session_state.get("role", "manager")
        st.markdown(
            f"<div style='text-align:right;color:#94a3b8;font-size:0.85rem'>"
            f"👤 <b style='color:#e2e8f0'>{username}</b> &nbsp;·&nbsp; {role}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if st.button("Logout", use_container_width=True, key="btn_logout"):
            logout()
    st.markdown("---")


# ─── Tab: Projects ─────────────────────────────────────────────────────────────

def tab_projects():
    st.markdown("## 📁 My Projects")
    st.markdown(
        "<div class='glass-card' style='color:#94a3b8'>"
        "Register a repository path below. The system will watch it for Python file changes "
        "and automatically analyze it. You'll receive a notification when actions need your review."
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Add project form ──
    with st.expander("➕ Register New Project", expanded=False):
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            p_name = st.text_input("Project Name", placeholder="My Backend API", key="p_name")
        with c2:
            p_path = st.text_input("Repository Path", placeholder="/mnt/e/My_work/my_repo", key="p_path")
        with c3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("Add Project", type="primary", use_container_width=True):
                if not p_name or not p_path:
                    st.error("Name and path are required.")
                else:
                    result = _post("/api/projects", json={"name": p_name, "repo_path": p_path})
                    if result and "error" not in result:
                        st.success(f"✅ Project '{p_name}' registered and watching started!")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        err = result.get("error", "Unknown error") if result else "API error"
                        st.error(f"❌ {err}")

    st.markdown("---")

    projects = _get("/api/projects") or []

    if not projects:
        st.markdown(
            "<div style='text-align:center;padding:3rem;color:#475569'>"
            "<div style='font-size:3rem'>📂</div>"
            "<div style='margin-top:0.5rem'>No projects yet. Register one above.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        return

    for proj in projects:
        pid    = proj["project_id"]
        name   = proj["name"]
        path   = proj["repo_path"]
        status = proj.get("status", "watching")
        last   = proj.get("last_analysis") or "Never"
        if last != "Never":
            try:
                last = datetime.fromisoformat(last).strftime("%d %b %Y, %H:%M")
            except Exception:
                pass

        dot = status_dot(status)

        col_info, col_actions = st.columns([5, 2])
        with col_info:
            st.markdown(
                f"<div class='project-card'>"
                f"<div style='font-size:1.1rem;font-weight:700;color:#e2e8f0'>{dot}{name}</div>"
                f"<div style='color:#64748b;font-size:0.85rem;margin-top:4px'>📂 {path}</div>"
                f"<div style='color:#64748b;font-size:0.82rem;margin-top:2px'>"
                f"Last analysis: {last} &nbsp;·&nbsp; Status: {status}"
                f"</div></div>",
                unsafe_allow_html=True,
            )
        with col_actions:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="btn-danger">', unsafe_allow_html=True)
            if st.button("🗑 Remove", key=f"del_{pid}", use_container_width=True):
                _delete(f"/api/projects/{pid}")
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)


# ─── Re-analysis helper ────────────────────────────────────────────────────────

def _trigger_reanalysis():
    """Trigger a fresh analysis on every registered project, then refresh the UI."""
    projects = _get("/api/projects") or []
    if not projects:
        st.info("No projects to analyze.")
        st.rerun()
        return

    triggered = 0
    for proj in projects:
        result = _post(f"/api/projects/{proj['project_id']}/analyze")
        if result and "error" not in result:
            triggered += 1

    if triggered:
        st.success(
            f"✅ Re-analysis started for {triggered} project(s). "
            "New findings will appear in the queue in a few seconds."
        )
        time.sleep(4)
    else:
        st.warning("Could not start analysis — check the API server.")
        time.sleep(1)

    st.rerun()


# ─── Tab: Review Queue ─────────────────────────────────────────────────────────

def tab_reviews():
    st.markdown("## ✅ Review Queue")

    groups = _get("/api/reviews/grouped") or []
    total_pending = sum(g["total"]   for g in groups)
    total_ready   = sum(g["ready"]   for g in groups)
    total_blocked = sum(g["blocked"] for g in groups)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pending",        total_pending)
    c2.metric("Ready to Review", total_ready)
    c3.metric("Blocked",        total_blocked)
    with c4:
        if st.button("🔁 Refresh", use_container_width=True):
            st.rerun()
    with c5:
        if st.button("🔄 Re-analyze", use_container_width=True):
            _trigger_reanalysis()

    if not groups:
        # Check if any project is mid-analysis — if so, auto-poll until done
        projects = _get("/api/projects") or []
        analyzing = any(p.get("status") == "analyzing" for p in projects)

        if analyzing:
            st.info("⏳ Analysis in progress — results will appear automatically...")
            time.sleep(3)
            st.rerun()
        elif projects:
            # Projects exist but no reviews yet — offer a one-click trigger
            st.markdown(
                "<div style='text-align:center;padding:2rem;color:#475569'>"
                "<div style='font-size:3rem'>📋</div>"
                "<div style='margin-top:0.5rem'>No pending reviews yet.</div>"
                "<div style='font-size:0.85rem;margin-top:0.4rem'>Click <b>Re-analyze</b> to scan your projects.</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='text-align:center;padding:3rem;color:#475569'>"
                "<div style='font-size:3rem'>🎉</div>"
                "<div style='margin-top:0.5rem'>No pending reviews — all clear!</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        return

    st.info(
        "Actions are grouped by file in safe execution order: "
        "**Security Fix** → **Delete Import** → **Delete Function** → **Refactor** → **Docstring** → **Restructure**. "
        "🔒 Blocked actions require completing higher-priority actions first."
    )
    st.markdown("---")

    for group in groups:
        file_name    = group["file_name"]
        file_path    = group["file_path"]
        n_ready      = group["ready"]
        n_blocked    = group["blocked"]

        status_line = f"  •  {n_ready} ready"
        if n_blocked:
            status_line += f"  •  🔒 {n_blocked} blocked"

        st.markdown(
            f"<div class='glass-card'>"
            f"<span style='font-size:1rem;font-weight:700;color:#e2e8f0'>📄 {file_name}</span>"
            f"<span style='color:#64748b;font-size:0.85rem'>{status_line}</span><br>"
            f"<span style='color:#475569;font-size:0.78rem'>{file_path}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        for step_num, item in enumerate(group["actions"]):
            action       = item["action"]
            is_blocked   = item["is_blocked"]
            block_reason = item["block_reason"]
            review_id    = item["id"]
            risk_level   = action["risk_level"].lower()
            atype        = action["action_type"].replace("_", " ").title()
            conf_pct     = int(action["confidence"] * 100)
            sub_actions  = action.get("impact_analysis", {}).get("sub_actions", [])

            step_icon = "🔒" if is_blocked else f"**{step_num+1}.**"
            label = (
                f"{step_icon} {atype} — {action['description'][:65]}"
                + ("…" if len(action["description"]) > 65 else "")
                + (" 🔒" if is_blocked else "")
            )

            with st.expander(label, expanded=(not is_blocked and step_num == 0)):
                if is_blocked:
                    st.warning(f"🔒 **Blocked** — {block_reason}")

                left, right = st.columns([3, 2])
                with left:
                    conf_color = "#4ade80" if conf_pct >= 70 else "#fbbf24" if conf_pct >= 50 else "#f87171"
                    st.markdown(
                        f"{risk_badge(risk_level)} &nbsp;"
                        f"<span style='color:#94a3b8'>Confidence: </span>"
                        f"<b style='color:{conf_color}'>{conf_pct}%</b><br><br>"
                        f"<b>Target:</b> <code>{action['target']}</code><br>"
                        f"<b>Reasoning:</b> {action['reasoning']}",
                        unsafe_allow_html=True,
                    )
                    if sub_actions and len(sub_actions) > 1:
                        st.markdown(
                            "**🔗 Will execute in order:**  \n"
                            + "  \n".join(f"  **Step {i+1}:** `{s}`" for i, s in enumerate(sub_actions))
                        )
                    if action.get("impact_analysis"):
                        if st.checkbox("📊 Show Impact Analysis", key=f"imp_{review_id}"):
                            st.json(action["impact_analysis"])

                with right:
                    notes = st.text_area(
                        "Notes",
                        placeholder="Optional comment...",
                        key=f"notes_{review_id}",
                        height=70,
                        label_visibility="collapsed",
                    )
                    col_a, col_r = st.columns(2, vertical_alignment="center")

                    with col_a:
                        if st.button(
                            "✅ Approve",
                            key=f"approve_{review_id}",
                            use_container_width=True,
                            type="primary",
                            disabled=is_blocked,
                            help=block_reason if is_blocked else None,
                        ):
                            _handle_approve(review_id, action, notes, sub_actions)

                    with col_r:
                        clicked_reject = st.button(
                            "❌ Reject",
                            key=f"reject_{review_id}",
                            use_container_width=True,
                        )
                        if clicked_reject:
                            result = _post(
                                f"/api/reviews/{review_id}/decision",
                                params={"dry_run": False, "execute_immediately": True},
                                json={"decision": "reject", "notes": notes or "Rejected by reviewer"},
                            )
                            if result and "error" not in result:
                                st.warning("❌ Rejected")
                                time.sleep(0.8)
                                st.rerun()
                            else:
                                st.error(result.get("error", "Error") if result else "Error")

        st.markdown("---")


def _handle_approve(review_id, action, notes, sub_actions):
    with st.spinner("Submitting approval..."):
        result = _post(
            f"/api/reviews/{review_id}/decision",
            params={"dry_run": False, "execute_immediately": True},
            json={"decision": "approve", "notes": notes},
        )

    if not result or "error" in result:
        st.error(result.get("error", "API error") if result else "API error")
        return

    if result.get("async"):
        _poll_async_job(result["job_id"], action, sub_actions)
    else:
        exec_res = result.get("execution_result", {})
        if exec_res and exec_res.get("success"):
            st.success("✅ Executed successfully!")
            n_inv = exec_res.get("invalidated_count", 0)
            if n_inv:
                st.info(f"🚫 {n_inv} stale action(s) automatically removed from queue.")
        elif exec_res:
            st.warning(f"⚠️ {exec_res.get('error', 'Check logs')}")
        else:
            st.success("✅ Approved")
        time.sleep(1)
        st.rerun()


def _poll_async_job(job_id, action, sub_actions):
    action_type  = action.get("action_type", "LLM action")
    total_steps  = len(sub_actions) if len(sub_actions) > 1 else 1
    is_multi     = total_steps > 1

    st.info(f"⏳ Running {action_type} via Ollama…")
    progress_bar = st.progress(0)
    status_text  = st.empty()

    max_polls = 45
    job_result = None
    last_step  = 0

    for poll_i in range(max_polls):
        time.sleep(8)
        job = _get(f"/api/jobs/{job_id}") or {}
        job_status   = job.get("status", "unknown")
        current_step = job.get("current_step", 1)
        elapsed      = (poll_i + 1) * 8

        if is_multi and current_step != last_step:
            last_step = current_step

        pct = min(max(int((current_step - 1) / total_steps * 100),
                      int(poll_i / max_polls * 100)), 95)
        progress_bar.progress(pct)
        status_text.markdown(f"⏳ {job_status} · Step {current_step}/{total_steps} · {elapsed}s")

        if job_status in ("done", "partial", "error"):
            job_result = job.get("execution_result", {})
            progress_bar.progress(100)
            break

    status_text.empty()

    if job_result is None:
        st.warning(f"⏰ Still running — poll `/api/jobs/{job_id}`")
        return

    if is_multi:
        steps = job_result.get("steps", [])
        n_ok  = sum(1 for s in steps if s.get("success"))
        if n_ok == len(steps):
            st.success(f"✅ All {len(steps)} steps completed!")
        else:
            st.warning(f"⚠️ {n_ok}/{len(steps)} steps succeeded")
        for s in steps:
            icon = "⏭️" if s.get("skipped") else ("✅" if s.get("success") else "⚠️")
            st.caption(f"{icon} Step {s['step']}: {s['action']}")
    else:
        if job_result.get("success"):
            st.success(f"✅ {action_type} completed!")
        else:
            st.error(f"❌ {job_result.get('error', 'Failed')}")

    if job_result.get("invalidated_reviews"):
        st.info(f"🚫 {job_result['invalidated_count']} stale action(s) removed.")
    time.sleep(2)
    st.rerun()


# ─── Tab: Notifications ───────────────────────────────────────────────────────

def tab_notifications(notif_data: dict = None):
    st.markdown("## 🔔 Notifications")

    col_title, col_btn = st.columns([4, 1])
    with col_btn:
        if st.button("Mark all read", use_container_width=True):
            _post("/api/notifications/read-all")
            st.rerun()

    data   = notif_data or fetch_notifications()
    notifs = data.get("notifications", [])

    if not notifs:
        st.markdown(
            "<div style='text-align:center;padding:3rem;color:#475569'>"
            "<div style='font-size:3rem'>🔕</div>"
            "<div style='margin-top:0.5rem'>No notifications yet.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        return

    for n in notifs:
        is_read  = n.get("read", False)
        border   = "#2d3748" if is_read else "#6366f1"
        opacity  = "0.5" if is_read else "1"
        created  = n.get("created_at", "")
        try:
            created = datetime.fromisoformat(created).strftime("%d %b %Y, %H:%M")
        except Exception:
            pass

        col_notif, col_action = st.columns([5, 1])
        with col_notif:
            st.markdown(
                f"<div class='glass-card' style='border-color:{border};opacity:{opacity}'>"
                f"<div style='font-weight:700;color:#e2e8f0'>"
                f"{'🔵' if not is_read else '⚪'} {n['message']}"
                f"</div>"
                f"<div style='color:#64748b;font-size:0.82rem;margin-top:4px'>"
                f"📁 {n.get('project_name','?')} &nbsp;·&nbsp; "
                f"🔍 {n.get('findings_count',0)} findings &nbsp;·&nbsp; "
                f"⏳ {n.get('pending_count',0)} pending &nbsp;·&nbsp; {created}"
                f"</div></div>",
                unsafe_allow_html=True,
            )
        with col_action:
            st.markdown("<br>", unsafe_allow_html=True)
            if not is_read:
                if st.button("Mark read", key=f"read_{n['id']}", use_container_width=True):
                    _post(f"/api/notifications/{n['id']}/read")
                    st.rerun()


# ─── Tab: Dashboard ───────────────────────────────────────────────────────────

def tab_dashboard():
    st.markdown("## 📊 Dashboard")

    stats   = _get("/api/stats") or {}
    q_stats = stats.get("review_queue", {})

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Sessions",  stats.get("total_sessions", 0))
    c2.metric("Pending Reviews", q_stats.get("pending", 0))
    c3.metric("Completed",       q_stats.get("completed", 0))
    c4.metric("Approved",        q_stats.get("approved", 0))
    c5.metric("Approval Rate",   f"{int(q_stats.get('approval_rate', 0)*100)}%")

    st.markdown("---")
    st.markdown("### 🏗️ Projects Overview")

    projects = _get("/api/projects") or []
    if projects:
        for proj in projects:
            status = proj.get("status", "watching")
            dot    = status_dot(status)
            last   = proj.get("last_analysis") or "Never"
            if last != "Never":
                try:
                    last = datetime.fromisoformat(last).strftime("%d %b %Y, %H:%M")
                except Exception:
                    pass
            st.markdown(
                f"<div class='glass-card'>"
                f"{dot}<b>{proj['name']}</b> &nbsp;·&nbsp; "
                f"<span style='color:#64748b'>{proj['repo_path']}</span> &nbsp;·&nbsp; "
                f"Last: {last} &nbsp;·&nbsp; Status: {status}"
                f"</div>",
                unsafe_allow_html=True,
            )
    else:
        st.info("No projects registered yet.")

    st.markdown("---")
    st.markdown("### 📜 Reviewer History")

    reviewers = _get("/api/reviews/history/reviewers")
    if reviewers and reviewers.get("reviewers"):
        selected = st.selectbox("Select reviewer", reviewers["reviewers"])
        if selected:
            hist = _get(f"/api/reviews/history/{selected}")
            if hist:
                h1, h2, h3, h4 = st.columns(4)
                h1.metric("Total",         hist["total"])
                h2.metric("Approved",      hist["approved"])
                h3.metric("Rejected",      hist["rejected"])
                h4.metric("Approval Rate", f"{int(hist['approval_rate']*100)}%")

                st.markdown("#### Recent Decisions")
                for entry in hist["history"][:10]:
                    icon = "✅" if entry["was_approved"] else "❌"
                    ts   = entry["created_at"][:16].replace("T", " ")
                    st.markdown(
                        f"<div class='glass-card' style='padding:0.7rem 1rem'>"
                        f"{icon} <b>{entry['action_type']}</b> &nbsp;·&nbsp; "
                        f"<span style='color:#64748b'>{entry['action_data'].get('target','?')}</span> &nbsp;·&nbsp; "
                        f"<span style='color:#475569;font-size:0.82rem'>{ts}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
    else:
        st.info("No review history found.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not api_ok():
        st.error("⚠️ **API Server Not Running**")
        st.code("uvicorn api.main:app --reload --host 0.0.0.0 --port 8000", language="bash")
        return

    if not is_authenticated():
        show_auth_page()
        return

    notif_data  = fetch_notifications()
    unread      = notif_data.get("unread_count", 0)
    show_header(unread)

    notif_label = f"🔔 Notifications {'🔴' if unread else ''}"

    tab1, tab2, tab3, tab4 = st.tabs([
        "📁 Projects",
        "✅ Review Queue",
        notif_label,
        "📊 Dashboard",
    ])

    with tab1: tab_projects()
    with tab2: tab_reviews()
    with tab3: tab_notifications(notif_data)
    with tab4: tab_dashboard()


if __name__ == "__main__":
    main()
