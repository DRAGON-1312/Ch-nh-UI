import streamlit as st
import requests
import firebase_admin
import base64
from firebase_admin import credentials, auth, firestore
import json
from pathlib import Path
import math
import folium
from streamlit_folium import st_folium
from backend_client import (
    search_ranked, route_preview, origin_suggest_sync, 
    chatbot_ask, intake_origin, ip_location
)
import time  # <-- thêm để debounce
import re
from streamlit_js_eval import get_geolocation

st.set_page_config(page_title="NestFeast", layout="wide")

# --- mapping lựa chọn UI → tags/mode ---
TAG_MAP = {
    "Hotel": ["hotel"],
    "Restaurant": ["restaurant"],
    "Cafe": ["cafe"],
}

MODE_MAP = {
    "driving": "driving",
    "walking": "walking",
    "motorcycling": "motorcycling",
    "truck": "truck"
}

# Map helpers
def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_num(x):
    return _to_float(x) is not None


def _get_center(items, origin):
    if origin and _is_num(origin.get("lat")) and _is_num(origin.get("lng")):
        return [float(origin["lat"]), float(origin["lng"])]
    for p in items or []:
        if _is_num(p.get("lat")) and _is_num(p.get("lng")):
            return [float(p["lat"]), float(p["lng"])]
    # Fallback: HCM
    return [10.776889, 106.700806]


def pick_lat_lng(obj: dict | None):
    """Trả về (lat, lng) từ obj theo cả hai dạng:
       {lat,lng} hoặc {location:{lat,lng}}; ép float & chống None."""
    if not obj:
        return (None, None)
    lat = obj.get("lat")
    lng = obj.get("lng")
    if lat is None or lng is None:
        loc = obj.get("location") or {}
        lat = loc.get("lat", lat)
        lng = loc.get("lng", lng)
    try:
        return (float(lat), float(lng))
    except (TypeError, ValueError):
        return (None, None)


# ---- Format helpers for ETA/Distance ----
def _fmt_eta(eta_s):
    x = _to_float(eta_s)
    if x is None:
        return None
    x = int(x)
    if x < 60 and x > 0:
        return "<1 min"
    return f"{x // 60} min"


def _fmt_dist(meters):
    d = _to_float(meters)
    if d is None:
        return None
    if d >= 1000:
        return f"{d / 1000:.1f} km"
    return f"{int(d)} m"


# Helpers for map
def render_osm_map(center_lat, center_lng, items, height_px: int = 420, zoom_start: int = 14, pickable: bool = False):
    """Render OpenStreetMap (Leaflet/Folium) trong cột bản đồ của Streamlit."""

    def _is_num_local(x):
        try:
            float(x)
            return True
        except (TypeError, ValueError):
            return False

    # Chọn tâm map: ưu tiên origin, rồi tới item đầu, cuối cùng fallback HCM
    if _is_num_local(center_lat) and _is_num_local(center_lng):
        center = [float(center_lat), float(center_lng)]
    else:
        center = None
        for p in items or []:
            lat, lng = pick_lat_lng(p)          # 🔧 dùng helper
            if _is_num_local(lat) and _is_num_local(lng):
                center = [float(lat), float(lng)]
                break
        if center is None:
            center = [10.776889, 106.700806]  # fallback HCM

    m = folium.Map(location=center, zoom_start=zoom_start, tiles="OpenStreetMap")

    # Marker origin (xanh lá) nếu hợp lệ
    if _is_num_local(center_lat) and _is_num_local(center_lng):
        folium.Marker(
            [float(center_lat), float(center_lng)],
            tooltip="Origin",
            icon=folium.Icon(color="green"),
        ).add_to(m)

    # Marker các điểm (cam)
    for i, p in enumerate(items or [], 1):
        lat, lng = pick_lat_lng(p)             # 🔧 dùng helper
        if not (_is_num_local(lat) and _is_num_local(lng)):
            continue
        name = p.get("name", "Unknown")
        addr = p.get("display_address", "")
        hint = p.get("hint") or ""
        popup_html = f"<b>{i}. {name}</b><br/>{addr}" + (f"<br/>{hint}" if hint else "")
        folium.Marker(
            [float(lat), float(lng)],
            tooltip=f"{i}. {name}",
            popup=popup_html,
            icon=folium.Icon(color="orange"),
        ).add_to(m)

    ret = st_folium(m, height=height_px, width=None)

    # ⬇️ Nếu đang bật pickable thì nhận click để đặt origin
    if pickable:
        click = (ret or {}).get("last_clicked")
        if isinstance(click, dict) and "lat" in click and "lng" in click:
            lat = float(click["lat"])
            lng = float(click["lng"])

            # ⚠️ Chỉ xử lý khi là 1 lần click MỚI (tránh auto-repeat sau rerun)
            last = st.session_state.get("nf_last_map_click")
            if (
                isinstance(last, dict)
                and abs(last.get("lat", 0.0) - lat) < 1e-6
                and abs(last.get("lng", 0.0) - lng) < 1e-6
            ):
                return

            st.session_state.nf_last_map_click = {"lat": lat, "lng": lng}

            display = f"{lat:.6f}, {lng:.6f}"
            place_id = None
            try:
                resp = intake_origin(lat=lat, lng=lng)  # dùng endpoint intake/origin
                ori = (resp or {}).get("origin") or {}
                display = ori.get("display_address") or display
                place_id = ori.get("place_id")
            except Exception:
                pass

            st.session_state.origin_selected = {
                "lat": lat,
                "lng": lng,
                "kind": "point",
                "display_address": display,
                "place_id": place_id,
            }
            st.session_state.origin_text = display
            st.session_state.prev_origin_text = display

            st.session_state.origin_locked = True
            st.session_state.ctx_confirmed = True
            st.session_state.chat_origin_text = display
            st.session_state._nf_chat_origin_next = display

            st.session_state.route_preview = None

            try:
                st.toast("Origin selected on map!", icon="✅")
            except Exception:
                st.success("Origin selected on map!")

            st.rerun()
    
    
def render_route_map(origin: dict, dest: dict, rp: dict, height_px: int = 420):
    """Vẽ polyline route + 2 marker bằng Folium/OSM."""
    # lấy center từ bounds nếu có
    bounds = (rp.get("geometry") or {}).get("bounds") or [origin["lat"], origin["lng"], dest["lat"], dest["lng"]]
    bmin_lat, bmin_lng, bmax_lat, bmax_lng = bounds
    center = [(bmin_lat + bmax_lat) / 2.0, (bmin_lng + bmax_lng) / 2.0]

    m = folium.Map(location=center, zoom_start=14, tiles="OpenStreetMap")

    # origin/dest markers
    folium.Marker([origin["lat"], origin["lng"]], tooltip="Origin",
                  icon=folium.Icon(color="green")).add_to(m)
    folium.Marker([dest["lat"], dest["lng"]], tooltip="Destination",
                  icon=folium.Icon(color="red")).add_to(m)

    # polyline
    coords = ((rp.get("geometry") or {}).get("coordinates")) or []
    if coords:
        folium.PolyLine(coords, weight=5, opacity=0.95).add_to(m)

    st_folium(m, height=height_px, width=None)


# My location (IP-based)
def _get_my_location():
    """
    Prioritize retrieving coordinates from Browser Geolocation (most accurate).
    If it fails → fallback to IP service (network node or ISP)
    Return: (lat, lng, accuracy_m, source); accuracy_m may be None if using IP.
    """
    try:
        loc = get_geolocation(timeout=5000)  # ms
        if loc and "coords" in loc:
            c = loc["coords"]
            lat = c.get("latitude")
            lng = c.get("longitude")
            acc = c.get("accuracy")  
            if lat is not None and lng is not None:
                return float(lat), float(lng), float(acc or 0), "browser"
    except Exception:
        pass
    # Fallback: IP-based 
    try:
        ip = ip_location()  
        lat = float(ip.get("lat") or ip.get("latitude"))
        lng = float(ip.get("lng") or ip.get("longitude"))
        return lat, lng, None, "ip"
    except Exception:
        return None, None, None, "none"


def _nf_reset_chat_ctx():
    st.session_state.ctx_confirmed = False
    st.session_state.chat_last_chips = []
    st.session_state.chat_origin_text = ""
    # mở lại autocomplete & xoá chọn cũ
    st.session_state.origin_locked = False
    st.session_state.origin_selected = None
    # đóng mọi menu mini
    st.session_state.nf_show_radius_menu = False
    st.session_state.nf_show_mode_menu   = False
    st.session_state.nf_show_origin_menu = False
    # reset kết quả list/route cũ (nếu có)
    st.session_state.res = None
    st.session_state.pending_cands = None
    st.session_state.route_preview = None
    
    
# === Firebase setup ===
FIREBASE_API_KEY = st.secrets["firebase_login"]["apiKey"]

if not firebase_admin._apps:
    cred = credentials.Certificate(dict(st.secrets["firebase_admin"]))
    firebase_admin.initialize_app(cred)
db = firestore.client()


def firebase_sign_in(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = {"email": email, "password": password, "returnSecureToken": True}
    return requests.post(url, json=payload).json()


def load_history(user_email):
    history_ref = db.collection("users").document(user_email).collection("history").order_by("timestamp")
    history_docs = history_ref.stream()
    history_list = []
    for doc in history_docs:
        data = doc.to_dict()
        data["timestamp"] = data["timestamp"].strftime("%d-%m-%Y %H:%M:%S")
        history_list.append(data)
    return history_list


# ======= Session state =======
if "user" not in st.session_state:
    st.session_state["user"] = None
if "history" not in st.session_state:
    st.session_state["history"] = []
if "show_login" not in st.session_state:
    st.session_state.show_login = False
if "show_register" not in st.session_state:
    st.session_state.show_register = False
if "res" not in st.session_state:
    st.session_state.res = None
if "pending_cands" not in st.session_state:
    st.session_state.pending_cands = None
if "last_query" not in st.session_state:
    st.session_state.last_query = {}
if "route_preview" not in st.session_state:
    st.session_state.route_preview = None  # sẽ lưu {"dest_idx": int, "rp": dict, "dest": {...}}

# Autocomplete origin
if "origin_suggestions" not in st.session_state:
    st.session_state.origin_suggestions = []
if "origin_selected" not in st.session_state:
    st.session_state.origin_selected = None
if "origin_text" not in st.session_state:
    st.session_state.origin_text = ""
    
# Origin riêng cho CHATBOT (không đụng tới form chính)
if "chat_origin_text" not in st.session_state:
    st.session_state.chat_origin_text = ""

# Thêm state cho debounce + cache + detect change
if "origin_text_input" not in st.session_state:
    st.session_state.origin_text_input = st.session_state.origin_text
if "prev_origin_text" not in st.session_state:
    st.session_state.prev_origin_text = st.session_state.origin_text
if "autocomplete_cache" not in st.session_state:
    st.session_state.autocomplete_cache = {}
if "last_auto_q" not in st.session_state:
    st.session_state.last_auto_q = ""
if "last_auto_ts" not in st.session_state:
    st.session_state.last_auto_ts = 0.0
if "origin_locked" not in st.session_state:
    st.session_state.origin_locked = False
    
# ⬇️ NEW: chỉ khi user bấm “Lưu & bắt đầu” mới coi là sẵn sàng
if "ctx_confirmed" not in st.session_state:
    st.session_state.ctx_confirmed = False
    
# ======= Chatbot session state =======
if "chat_open" not in st.session_state:
    st.session_state.chat_open = True  # mở sẵn khung chat
if "chat_msgs" not in st.session_state:
    st.session_state.chat_msgs = [
        {"role": "assistant", "content": "Hi! I can suggest nearby places or show **details #n** of a place."}
    ]
if "chat_last_chips" not in st.session_state:
    st.session_state.chat_last_chips = []  # nhớ chips để render lại nút bấm
    
# --- toggles cho menu nhỏ trong chat ---
if "nf_show_radius_menu" not in st.session_state:
    st.session_state.nf_show_radius_menu = False
if "nf_show_mode_menu" not in st.session_state:
    st.session_state.nf_show_mode_menu = False
if "nf_show_origin_menu" not in st.session_state:
    st.session_state.nf_show_origin_menu = False
    
# --- NEW: pick origin on map toggle
if "nf_pick_on_map" not in st.session_state:
    st.session_state.nf_pick_on_map = False

if "nf_debug_ctx" not in st.session_state:
    st.session_state.nf_debug_ctx = False

# --- NEW: pick origin on map toggle
if "nf_pick_on_map" not in st.session_state:
    st.session_state.nf_pick_on_map = False

# ⚠️ NEW: nhớ click cuối cùng trên map để tránh auto-repeat
if "nf_last_map_click" not in st.session_state:
    st.session_state.nf_last_map_click = None

if "nf_debug_ctx" not in st.session_state:
    st.session_state.nf_debug_ctx = False
    
# Lấy page từ query param
params = st.query_params
page = params.get("page", None)

# Quyết định hiển thị form
st.session_state.show_login = (page == "login")
st.session_state.show_register = (page == "register")


def get_base64_image(path: str | Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


ASSETS_DIR = Path(__file__).resolve().parent / ".image"
IMG_PATH = ASSETS_DIR / "logo.jpg"

img_base64 = ""
if IMG_PATH.exists():
    img_base64 = get_base64_image(IMG_PATH)

logo_tag = f'<img src="data:image/jpeg;base64,{img_base64}" width="100">' if img_base64 else ""
img_html = f'<img src="data:image/jpeg;base64,{img_base64}" width="56" style="border-radius:8px;">' if img_base64 else ""

st.markdown(f"""
<div class="nf-nav">
  <div class="brand">
    {img_html}
    <span>NestFeast</span>
  </div>
  <div class="auth">
    <a href="?page=login">Login</a>
    <a href="?page=register">Register</a>
  </div>
</div>

<style>
.nf-nav {{
  display:flex; justify-content:space-between; align-items:center;
  background:#2F4F3A; padding:16px 24px; border-radius:12px;
}}
.nf-nav .brand {{
  display:flex; align-items:center; gap:12px;
  color:#fff; font-weight:800; font-size:28px;
}}
.nf-nav .auth a {{
  color:#fff; text-decoration:none; font-size:16px; margin-left:20px;
}}
</style>
""", unsafe_allow_html=True)

# --- LOGIN FORM ---
if st.session_state.show_login:
    st.markdown("""
        <style>
        MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        </style>
        """, unsafe_allow_html=True)

    st.subheader("Login")
    login_email = st.text_input("Email", key="login_email")
    login_pass = st.text_input("Password", type="password", key="login_pass")

    if st.button("Login"):
        result = firebase_sign_in(login_email, login_pass)

        if "error" in result:
            message = result["error"]["message"]
            if message == "EMAIL_NOT_FOUND":
                st.error("This email is not registered. Please register first.")
            elif message == "INVALID_PASSWORD":
                st.error("Incorrect password. Please try again.")
            else:
                st.error(f"Login error: {message}")
        else:
            st.session_state.user = login_email
            st.success(f"Login successful: {login_email}")
            st.query_params.update({"page": "home"})
            st.rerun()

    st.stop()

# --- REGISTER FORM ---
if st.session_state.show_register:
    st.markdown("""
        <style>
        MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        </style>
    """, unsafe_allow_html=True)

    st.subheader("Register")
    reg_email = st.text_input("Email", key="reg_email")
    reg_pass = st.text_input("Password", type="password", key="reg_pass")
    reg_cf = st.text_input("Confirm Password", type="password", key="reg_cf")

    if st.button("Create Account"):
        if reg_pass != reg_cf:
            st.error("Passwords do not match!")
        else:
            # Check if email already exists
            try:
                user_record = auth.get_user_by_email(reg_email)
                st.warning("This email is already registered. Please log in.")
            except firebase_admin._auth_utils.UserNotFoundError:
                # Email not found → create new user
                try:
                    user = auth.create_user(email=reg_email, password=reg_pass)
                    st.success("Registration successful! You can now log in.")
                    st.query_params["page"] = "login"
                    st.session_state.show_register = False
                    st.rerun()
                except Exception as e:
                    st.error(f"Registration error: {e}")

    st.stop()

# --- HOME PAGE ---
if page == "home":
    if st.session_state.user:
        st.write(f"Welcome {st.session_state.user}! This is the home page.")

        if st.button("Log out"):
            st.session_state.user = None
            st.query_params.update({"page": "home"})
            st.rerun()

# =========================
# CUSTOM CSS
# =========================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap');

* {
    font-family: 'Poppins', sans-serif;
}

MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 18px 40px;
    background-color: #2F4F3B;
}

.header-title {
    color: white;
    font-size: 32px;
    display: flex;
    align-items: center;
    gap: 1px;
    font-weight: 600;
}

.recommender-btn {
    background-color: #506654;
    color: green;
    padding: 10px 28px;
    border-radius: 6px;
    font-size: 18px;
    text-decoration: none;
}

.input-box {
    background: white;
    padding: 25px;
    border-radius: 40px;
    margin-top: 25px;
}

.input-title {
    font-size: 16px;
    font-weight: 600;
    color: #333;
    margin-bottom: 6px;
}

.chat-item {
    background: white;
    padding: 8px 12px;
    margin-top: 8px;
    border-radius: 14px;
    border: 1px solid #ccc;
    cursor: pointer;
}
</style>
""", unsafe_allow_html=True)

# --- Session state ---
if "search_clicked" not in st.session_state:
    st.session_state.search_clicked = False

# =========================
# INPUT AREA (type-ahead autocomplete)
# =========================
# Chỉ sync khi origin được set bằng code (Use this address),
# tránh đè lên text user đang gõ
if st.session_state.get("_sync_origin_next") or (
    st.session_state.origin_locked
    and st.session_state.origin_text != st.session_state.origin_text_input
):
    st.session_state.origin_text_input = st.session_state.origin_text
    st.session_state._sync_origin_next = False
        
# Init widget values once
if "nf_main_radius" not in st.session_state:
    st.session_state.nf_main_radius = int(st.session_state.get("radius_km", 3))
if "nf_main_mode" not in st.session_state:
    st.session_state.nf_main_mode = st.session_state.get("mode_label", "motorcycling")
if "nf_main_tag" not in st.session_state:
    st.session_state.nf_main_tag = st.session_state.get("opt_label", "Cafe")

# If backend/chat changed context, push those values to widgets once
if st.session_state.get("_nf_push_ctx_to_widgets"):
    st.session_state.nf_main_radius = int(st.session_state.get("radius_km", st.session_state.nf_main_radius))
    st.session_state.nf_main_mode   = st.session_state.get("mode_label",  st.session_state.nf_main_mode)
    st.session_state.nf_main_tag    = st.session_state.get("opt_label",   st.session_state.nf_main_tag)
    st.session_state._nf_push_ctx_to_widgets = False
    
col1, col2, col3, col4 = st.columns(4)
with col1:
    origin_text = st.text_input(
        "Origin",
        key="origin_text_input",
        placeholder="Enter address, location...",
    )

    # khi user đổi text → coi như origin chưa khóa + xoá kết quả/route cũ
    if origin_text != st.session_state.prev_origin_text:
        st.session_state.origin_selected = None
        st.session_state.prev_origin_text = origin_text
        st.session_state.origin_locked = False
        # ✨ reset thêm để tránh hiểu nhầm còn kết quả cũ
        st.session_state.res = None
        st.session_state.pending_cands = None
        st.session_state.route_preview = None

    st.session_state.origin_text = origin_text
    q = origin_text.strip()
    now = time.time()

    # type-ahead + debounce + cache
    if len(q) < 3 or st.session_state.origin_locked:
        # quá ngắn hoặc đã “lock origin” → không suggest
        st.session_state.origin_suggestions = []
    else:
        cache = st.session_state.autocomplete_cache
        key_q = q.lower()

        if key_q in cache:
            st.session_state.origin_suggestions = cache[key_q]
        elif q != st.session_state.last_auto_q and (now - st.session_state.last_auto_ts) > 0.5:
            try:
                with st.spinner("Suggesting..."):
                    suggestions = origin_suggest_sync(q)
                st.session_state.origin_suggestions = suggestions
                cache[key_q] = suggestions
                st.session_state.last_auto_q = q
                st.session_state.last_auto_ts = now
            except Exception:
                st.session_state.origin_suggestions = []

    
    # --- Button: dùng vị trí hiện tại (GPS/IP) ---
    if st.button("📍 My location", key="btn_my_loc"):
        lat, lng, acc, src = _get_my_location()
        if lat is None or lng is None:
            st.warning("Couldn't get your location. Please allow location in your browser / disable VPN / try again.")
        else:
            # Chuẩn hoá origin + lấy display_address, place_id từ backend
            resp = intake_origin(lat=lat, lng=lng)    # -> {origin, reason_code, ...}
            ori  = (resp or {}).get("origin") or {}
            display = ori.get("display_address") or f"{lat:.6f}, {lng:.6f}"

            # Lưu origin đầy đủ (kèm display_address, place_id)
            st.session_state.origin_selected = {
                "lat": float(lat), "lng": float(lng), "kind": "point",
                "display_address": display, "place_id": ori.get("place_id"),
            }
            st.session_state.origin_text = display
            st.session_state.prev_origin_text = display

            # ⬅️ QUAN TRỌNG: đồng bộ vào widget + khoá autocomplete + cho phép chat dùng ngay
            st.session_state.origin_locked = True
            st.session_state._sync_origin_next = True   # lần rerun tiếp theo sẽ push vào text_input

            st.session_state.route_preview = None

            try:
                st.toast("Using current location", icon="✅")
            except Exception:
                pass
            st.rerun()


with col2:
    if "nf_main_radius" in st.session_state:
        radius_km = st.number_input("Radius", min_value=1, max_value=7, step=1, key="nf_main_radius")
    else:
        radius_km = st.number_input(
            "Radius",
            min_value=1, max_value=7,
            value=int(st.session_state.get("radius_km", 3)),
            step=1, key="nf_main_radius",
        )

with col3:
    _m_opts = list(MODE_MAP.keys())
    if "nf_main_mode" in st.session_state:
        mode_label = st.selectbox("Transportation", _m_opts, key="nf_main_mode")
    else:
        mode_label = st.selectbox(
            "Transportation", _m_opts,
            index=_m_opts.index(st.session_state.get("mode_label", "motorcycling")),
            key="nf_main_mode",
        )

with col4:
    _t_opts = list(TAG_MAP.keys())
    if "nf_main_tag" in st.session_state:
        opt_label = st.selectbox("Options", _t_opts, key="nf_main_tag")
    else:
        opt_label = st.selectbox(
            "Options", _t_opts,
            index=_t_opts.index(st.session_state.get("opt_label", "Cafe")),
            key="nf_main_tag",
        )
    
    
# Always sync widget -> core state
st.session_state.radius_km = int(st.session_state.nf_main_radius)
st.session_state.mode_label = st.session_state.nf_main_mode
st.session_state.opt_label  = st.session_state.nf_main_tag

# --- Hiển thị danh sách gợi ý origin (nếu có) ---
if st.session_state.origin_suggestions:
    sug = st.session_state.origin_suggestions
    labels = [
        f"{i+1}. {s.get('main_text','')} — {s.get('secondary_text','')}"
        for i, s in enumerate(sug)
    ]
    idx = st.radio(
        "Select origin from suggestions:",
        options=range(len(labels)),
        format_func=lambda i: labels[i],
        key="origin_suggest_idx",
    )
    chosen = sug[idx]

    if st.button("Use this address", key="btn_use_origin"):
        # Lấy thông tin cơ bản từ suggestion
        display = chosen.get("description") or chosen.get("main_text") or ""
        place_id = chosen.get("place_id")
        lat, lng = pick_lat_lng(chosen)

        # 🔧 NEW: nếu thiếu lat/lng → gọi intake_origin(origin_text=...)
        if not (_is_num(lat) and _is_num(lng)):
            # thử chuẩn hoá lần cuối từ text
            try:
                norm = intake_origin(origin_text=display)
                ori  = (norm or {}).get("origin") or {}
                lat  = _to_float(ori.get("lat"))
                lng  = _to_float(ori.get("lng"))
                display  = ori.get("display_address") or display
                place_id = ori.get("place_id") or place_id
            except Exception:
                pass
            
        # Nếu vẫn không có toạ độ → KHÔNG lock, yêu cầu chọn lại
        if not (_is_num(lat) and _is_num(lng)):
            st.warning("This suggestion has no coordinates. Please pick another one or click on the map to set the origin.")
            st.stop()  # tránh set origin_selected sai

        # Lock origin + đồng bộ lại text
        st.session_state.origin_selected = {
            "lat": float(lat), "lng": float(lng), "kind": "point",
            "display_address": display,
            "place_id": place_id,
        }
        st.session_state.origin_text = display
        st.session_state.prev_origin_text = display

        # Khoá autocomplete + đẩy text lên ô input ở lần rerun kế tiếp
        st.session_state.origin_locked = True
        st.session_state.origin_suggestions = []
        st.session_state._sync_origin_next = True
        st.session_state.chat_origin_text = display
        st.session_state._nf_chat_origin_next = display

        st.rerun()


# Layout Map + Results
res = st.session_state.res

c_map, c_list = st.columns([1.2, 1])
status = st.empty()

# === ALWAYS-ON MAP (luôn hiện map, cho phép chọn origin bằng click) ===
with c_map:
    # Toggle cho phép pick trên map
    prev_pick = st.session_state.get("nf_pick_on_map", False)
    pick_flag = st.toggle(
        "🗺️ Pick origin on map",
        value=prev_pick,
        help="Click on the map to set the origin.",
    )
    st.session_state.nf_pick_on_map = pick_flag

    # Nếu tắt toggle → reset click cũ để lần sau bật lại có thể chọn cùng vị trí
    if not pick_flag:
        st.session_state.nf_last_map_click = None

    # Lấy dữ liệu cho map: items (nếu đã có kết quả) + origin hiện có
    items_for_map = (res or {}).get("items") or []

    origin_for_map = (
        st.session_state.get("origin_selected")
        or (res or {}).get("origin")
        or {}
    )

    # Toạ độ trung tâm: ưu tiên origin, fallback sang item đầu tiên (dùng pick_lat_lng)
    center_lat = center_lng = None
    if isinstance(origin_for_map, dict):
        center_lat, center_lng = pick_lat_lng(origin_for_map)

    if not (_is_num(center_lat) and _is_num(center_lng)):
        if items_for_map:
            center_lat, center_lng = pick_lat_lng(items_for_map[0])


    # Nếu đang ở chế độ xem route_preview và có đủ dữ liệu → vẽ route,
    # ngược lại luôn vẽ map điểm (origin + items)
    rp_state = st.session_state.get("route_preview")
    if rp_state and res and not st.session_state.pending_cands:
        # origin lấy từ kết quả gần nhất (ưu tiên), fallback origin_for_map
        o_lat, o_lng = pick_lat_lng((res or {}).get("origin") or origin_for_map or {})
        d_lat, d_lng = pick_lat_lng((rp_state or {}).get("dest") or {})
        rp = (rp_state or {}).get("rp") or {}
        if _is_num(o_lat) and _is_num(o_lng) and _is_num(d_lat) and _is_num(d_lng):
            render_route_map(
                origin={"lat": float(o_lat), "lng": float(o_lng)},
                dest={"lat": float(d_lat), "lng": float(d_lng)},
                rp=rp,
                height_px=420,
            )
        else:
            # Thiếu dữ liệu route → quay lại map điểm
            render_osm_map(
                center_lat, center_lng, items_for_map,
                height_px=420, zoom_start=14, pickable=pick_flag
            )
    else:
        # Map điểm bình thường (kể cả lúc chưa có kết quả nào)
        render_osm_map(
            center_lat, center_lng, items_for_map,
            height_px=420, zoom_start=14, pickable=pick_flag
        )


clicked = st.button("RECOMMENDER", type="primary")
if clicked:
    st.session_state.route_preview = None
    status.markdown("🟢 **RUNNING...**")

    # Lưu query chung
    st.session_state.last_query = dict(
        tags=TAG_MAP[st.session_state.opt_label],
        radius_m=int(st.session_state.radius_km * 1000),
        mode=MODE_MAP[st.session_state.mode_label],
        seed_cap=30, limit=20, lang="en", country="vn",
    )

    try:
        with st.spinner("Finding the best places..."):
            origin_payload = st.session_state.get("origin_selected")
            
            # FINAL ATTEMPT: nếu đã có origin_selected nhưng thiếu lat/lng → thử chuẩn hoá từ text
            if origin_payload and not (_is_num(origin_payload.get("lat")) and _is_num(origin_payload.get("lng"))):
                try:
                    norm = intake_origin(origin_text=st.session_state.get("origin_text",""))
                    ori  = (norm or {}).get("origin") or {}
                    lat  = _to_float(ori.get("lat")); lng = _to_float(ori.get("lng"))
                    if _is_num(lat) and _is_num(lng):
                        st.session_state.origin_selected.update({
                            "lat": float(lat), "lng": float(lng),
                            "display_address": ori.get("display_address") or st.session_state.get("origin_text",""),
                            "place_id": ori.get("place_id") or st.session_state.origin_selected.get("place_id"),
                        })
                        origin_payload = st.session_state.origin_selected
                except Exception:
                    pass

            # ✅ Ưu tiên lat/lng nếu đã lock hoặc text khớp (đỡ bị confirm lại)
            use_point = bool(
                origin_payload
                and _is_num(origin_payload.get("lat")) and _is_num(origin_payload.get("lng"))
                and (
                    st.session_state.get("origin_locked", False) or
                    (str(origin_payload.get("display_address", "")).strip()
                    == (st.session_state.get("origin_text", "") or "").strip())
                )
            )

            if use_point:
                res = search_ranked(
                    origin={
                        "lat": float(origin_payload["lat"]),
                        "lng": float(origin_payload["lng"]),
                        "kind": origin_payload.get("kind", "point"),
                        "display_address": origin_payload.get("display_address"),
                        "place_id": origin_payload.get("place_id"),
                    },
                    **st.session_state.last_query,
                )
            else:
                # Fallback an toàn: dùng origin_text hiện tại
                res = search_ranked(
                    origin_text=st.session_state.get("origin_text", ""),
                    **st.session_state.last_query
                )
    except Exception as e:
        status.error("❌ FAILED")
        st.exception(e)
        st.stop()
    else:
        status.success("✅ DONE")
        st.session_state.res = res
        st.session_state.pending_cands = res.get("candidates") if res.get("need_confirm") else None
        st.rerun()

# Nếu BE yêu cầu xác nhận origin → show radio + nút Confirm
if st.session_state.pending_cands:
    st.warning("Please confirm your starting point:")
    cands = st.session_state.pending_cands
    options = [f"{c['name']} — {c.get('display_address','')}" for c in cands]
    idx = st.radio("Candidates", options=range(len(options)),
                   format_func=lambda i: options[i], index=0, key="cand_idx")

    if st.button("Use this origin", key="confirm_origin"):
        chosen = cands[st.session_state.cand_idx]
        with st.spinner("Re-running with confirmed origin..."):
            res2 = search_ranked(
                origin={
                    "lat": chosen["lat"], "lng": chosen["lng"],
                    "kind": "point",
                    "display_address": chosen.get("display_address"),
                    "place_id": chosen.get("place_id"),
                },
                **st.session_state.last_query
            )
        st.session_state.res = res2
        st.session_state.pending_cands = None
        
        st.session_state.origin_selected = {
            "lat": chosen["lat"], "lng": chosen["lng"], "kind": "point",
            "display_address": chosen.get("display_address"),
            "place_id": chosen.get("place_id"),
        }
        st.session_state.origin_text = chosen.get("display_address") or ""
        st.session_state.prev_origin_text = st.session_state.origin_text
        st.session_state.origin_locked = True
        st.session_state.ctx_confirmed = True
        st.session_state.chat_origin_text = st.session_state.origin_text
        st.session_state._nf_chat_origin_next   = st.session_state.origin_text

        st.rerun()


# Helpers for chatbot
def _current_chat_context() -> dict:
    ctx = {
        "radius_km": float(st.session_state.get("radius_km", 2) or 2),
        "mode": MODE_MAP.get(st.session_state.get("mode_label", "motorcycling"), "motorcycling"),
        "tags": TAG_MAP.get(st.session_state.get("opt_label", "Cafe"), ["cafe"]),
        "lang": "en",
    }

    have_point = False

    # 1) Ưu tiên origin đã chọn (lat/lng).
    # Nếu user đã chọn address nhưng lat/lng đang NULL → thử chuẩn hoá 1 lần từ backend.
    ori_sel = st.session_state.get("origin_selected")
    if ori_sel and not (_is_num(ori_sel.get("lat")) and _is_num(ori_sel.get("lng"))):
        try:
            txt = (
                ori_sel.get("display_address")
                or st.session_state.get("origin_text")
                or st.session_state.get("origin_text_input")
                or ""
            ).strip()
            if len(txt) >= 3:
                norm = intake_origin(origin_text=txt)
                ori_norm = (norm or {}).get("origin") or {}
                lat2 = _to_float(ori_norm.get("lat"))
                lng2 = _to_float(ori_norm.get("lng"))
                if _is_num(lat2) and _is_num(lng2):
                    ori_sel = st.session_state.origin_selected = {
                        "lat": float(lat2),
                        "lng": float(lng2),
                        "kind": "point",
                        "display_address": ori_norm.get("display_address")
                            or ori_sel.get("display_address")
                            or txt,
                        "place_id": ori_norm.get("place_id") or ori_sel.get("place_id"),
                    }
        except Exception:
            pass

    if ori_sel and _is_num(ori_sel.get("lat")) and _is_num(ori_sel.get("lng")):
        ctx["origin"] = {
            "lat": float(ori_sel["lat"]),
            "lng": float(ori_sel["lng"]),
            "kind": "point",
        }
        have_point = True
    else:
        # 2) Fallback origin từ kết quả gần nhất (nếu có)
        res = st.session_state.get("res")
        if res and res.get("origin") and _is_num(res["origin"].get("lat")) and _is_num(res["origin"].get("lng")):
            ctx["origin"] = {
                "lat": float(res["origin"]["lat"]),
                "lng": float(res["origin"]["lng"]),
                "kind": "point",
            }
            have_point = True

    # --- Luôn gắn origin_text để BE có thể tự geocode nếu thiếu point ---
    ctx["origin_text"] = (
        st.session_state.get("chat_origin_text")
        or st.session_state.get("origin_text")
        or st.session_state.get("origin_text_input")
        or ""
    )

    return ctx
            

def _chip_to_prompt(chip: dict, lang: str = "en") -> str | None:
    it = (chip or {}).get("intent")
    pl = (chip or {}).get("payload") or {}

    # ƯU TIÊN: dùng text/label BE đã render để tránh lệch meaning
    if it == "change_radius":
        return pl.get("text") or chip.get("label")  # ví dụ: "Giảm bán kính 1 km", "Tăng bán kính 2 km"

    if it == "change_mode":
        # nếu BE đã đóng gói sẵn text/label thì dùng luôn
        s = pl.get("text") or chip.get("label")
        if s:
            return s
        # fallback: tự dựng câu như cũ
        m = pl.get("mode")
        if lang == "vi":
            vi = {"walking": "đi bộ", "motorcycling": "xe máy", "driving": "ô tô", "truck": "xe tải"}
            return f"tôi {vi.get(m, m)}"
        else:
            en = {"walking": "walk", "motorcycling": "motorcycle", "driving": "drive", "truck": "truck"}
            return en.get(m, m)

    if it == "pick_place" and "index" in pl:
        return f"view #{int(pl['index'])}"
    if it == "detail_place" and "index" in pl:
        return (f"details #{int(pl['index'])}" if lang == "en" else f"chi tiết #{int(pl['index'])}")

    # Xác nhận origin từ danh sách gợi ý
    if it in ("pick_origin", "use_origin", "confirm_origin") and "index" in pl:
        return f"use origin #{int(pl['index'])}"

    return None


def _starter_chips():
    tags = TAG_MAP.get(st.session_state.get("opt_label", "Cafe"), ["cafe"])
    if "cafe" in tags:
        nearby_label, nearby_msg = "Nearby cafes", "nearby cafes"
    elif "restaurant" in tags:
        nearby_label, nearby_msg = "Nearby restaurants", "nearby restaurants"
    else:
        nearby_label, nearby_msg = "Nearby hotels", "nearby hotels"

    return [
        {"label": nearby_label,        "intent": "free_text", "payload": {"text": nearby_msg}},
        {"label": "Change radius",     "intent": "ui_toggle", "payload": {"menu": "radius"}},
        {"label": "Change transport",  "intent": "ui_toggle", "payload": {"menu": "mode"}},
        # {"label": "Change origin",     "intent": "ui_toggle", "payload": {"menu": "origin"}},
    ]


def _send_or_prime(user_text: str) -> dict:
    """
    Nếu user gõ 'xem #n' / 'chi tiết #n' / 'details #n' mà CHƯA có list hiện hành,
    thì gửi 1 lượt '... gần đây' (nearby cafes / restaurants / hotels) trước
    rồi mới gửi câu người dùng.

    Nếu user bấm chip "Details #n" thì chắc chắn vừa có quick recommend
    (chat_last_chips đã chứa intent = detail_place) → KHÔNG cần prime nữa.
    """
    need_prime = False

    # Có pattern "details #n" / "chi tiết #n" / "xem #n" không?
    if re.search(r"(xem|chi\s*tiết|view|details)\s*#\d+", user_text, flags=re.I):
        # 1) Nếu vừa có quick recommend thì chat_last_chips sẽ có chip detail_place
        chips = st.session_state.get("chat_last_chips") or []
        has_detail_chip = any((c or {}).get("intent") == "detail_place" for c in chips)

        if not has_detail_chip:
            # 2) Thử dùng kết quả từ form lớn (res) nếu có
            res = st.session_state.get("res") or {}
            items = res.get("items") or []
            if not items:
                need_prime = True

    # ----- Prime nearby nếu thực sự cần -----
    if need_prime:
        tags = TAG_MAP.get(st.session_state.get("opt_label", "Cafe"), ["cafe"])
        if "cafe" in tags:
            prime = "nearby cafes"
        elif "restaurant" in tags:
            prime = "nearby restaurants"
        else:
            prime = "nearby hotels"

        prime_resp = chatbot_ask(
            message=prime,
            history=st.session_state.chat_msgs,
            context=_current_chat_context(),
        )
        _apply_ctx(prime_resp)
        st.session_state.chat_msgs.append(
            {"role": "assistant", "content": prime_resp.get("reply", "")}
        )
        st.session_state.chat_last_chips = prime_resp.get("chips") or []

    # ----- Tự confirm origin từ origin_text nếu thiếu point -----
    ctx = _current_chat_context()
    try:
        if (
            st.session_state.get("ctx_confirmed", False)
            and "origin" not in ctx
            and len((ctx.get("origin_text") or "").strip()) >= 3
        ):
            q = (ctx["origin_text"] or "").strip()
            sugs = origin_suggest_sync(q) or []
            if sugs:
                top = sugs[0]
                top_lat, top_lng = pick_lat_lng(top)
                if _is_num(top_lat) and _is_num(top_lng):
                    ctx["origin"] = {
                        "lat": float(top_lat),
                        "lng": float(top_lng),
                        "kind": "point",
                    }
                    st.session_state.origin_selected = {
                        "lat": float(top_lat),
                        "lng": float(top_lng),
                        "kind": "point",
                        "display_address": top.get("description")
                        or top.get("main_text"),
                        "place_id": top.get("place_id"),
                    }

                st.session_state.origin_text = (
                    top.get("description") or top.get("main_text") or q
                )
                st.session_state.prev_origin_text = st.session_state.origin_text
                st.session_state.origin_locked = True
                st.session_state._sync_origin_next = True
                st.session_state.chat_origin_text = st.session_state.origin_text
    except Exception:
        pass

    st.session_state["__last_ctx_debug"] = ctx  # (tiny debug aid)

    # Gửi chính câu người dùng cho BE
    return chatbot_ask(
        message=user_text,
        history=st.session_state.chat_msgs,
        context=ctx,
    )
    
    
def _apply_ctx(resp: dict):
    try:
        ctx = (resp or {}).get("context") or {}
        pushed = False

        if "radius_km" in ctx:
            r = int(float(ctx["radius_km"]))
            # core state cho BE
            st.session_state.radius_km = r
            pushed = True

        if "mode" in ctx:
            m_val = str(ctx["mode"])
            st.session_state.mode_label = m_val
            pushed = True

        # (tuỳ chọn) nếu BE trả về tags thì map sang Options
        if "tags" in ctx:
            tags = ctx.get("tags") or []
            lab = None
            if tags:
                t0 = str(tags[0]).lower()
                for label, tlist in TAG_MAP.items():
                    if t0 in [t.lower() for t in tlist]:
                        lab = label
                        break
            if lab:
                st.session_state.opt_label = lab
                pushed = True
                
        if isinstance(ctx.get("origin"), dict):
            o = ctx["origin"]
            if all(k in o for k in ("lat", "lng")):
                st.session_state.origin_selected = {
                    "lat": o["lat"], "lng": o["lng"], "kind": "point"
                }
                

        # tell main form to push core state -> widgets next rerun
        if pushed:
            st.session_state._nf_push_ctx_to_widgets = True

    except Exception:
        pass
    
    
def _context_ready() -> bool:
    # Cho chat khi user đã bấm "Lưu & bắt đầu"
    if not st.session_state.get("ctx_confirmed", False):
        return False

    # Và có ÍT NHẤT: lat/lng HOẶC origin_text >= 3 ký tự (để BE tự geocode)
    ctx = _current_chat_context()
    have_point = bool(ctx.get("origin"))
    have_text  = len((ctx.get("origin_text") or "").strip()) >= 3
    return have_point or have_text


def _onboarding_message() -> str:
    return (
        "To recommend accurately, I need your **origin**, **radius**, **transportation**, and **place type**.\n\n"
        "• *Origin*: enter an address (e.g., `University of Science, HCMC`)\n"
        "• *Radius*: search radius (km)\n"
        "• *Transportation*: walk / motorcycle / car\n"
        "• *Tag*: Restaurant / Cafe / Hotel\n\n"
        "Click **Save & start** to begin."
    )


# ==== Render kết quả + Map/Route ====
if res and not st.session_state.pending_cands:
    items  = res.get("items") or []
    origin = res.get("origin") or {}

    if not items:
        st.info("No result yet.")
    else:
        # -------- Cột LIST (bên phải) --------
        with c_list:
            st.subheader("Matched Results")
            
            # NÚT THOÁT CHẾ ĐỘ ROUTE → QUAY LẠI MAP ĐIỂM
            if st.session_state.get("route_preview") and st.button("🧭 Back to points map", key="rp_back"):
                st.session_state.route_preview = None
                st.rerun()  

            # Chọn 1 item để preview route
            name_opts = [f"{i+1}. {p.get('name','Unknown')}" for i, p in enumerate(items)]
            sel_idx = st.selectbox(
                "Preview route to:",
                range(len(items)),
                format_func=lambda i: name_opts[i],
                index=0,
            )

            # Nút preview route
            if st.button("Preview route", key="preview_btn"):
                o_lat, o_lng = pick_lat_lng(origin)
                d_lat, d_lng = pick_lat_lng(items[sel_idx])

                if o_lat is None or o_lng is None:
                    st.warning("Origin coordinates are missing — please confirm the origin.")
                elif d_lat is None or d_lng is None:
                    st.warning("Destination has invalid coordinates — try another item.")
                else:
                    try:
                        with st.spinner("Fetching route..."):
                            o    = {"lat": o_lat, "lng": o_lng}
                            dest = {"lat": d_lat, "lng": d_lng}
                            rp   = route_preview(o, dest, mode=MODE_MAP[st.session_state.mode_label])
                        st.session_state.route_preview = {"dest_idx": sel_idx, "rp": rp, "dest": dest}
                        st.rerun()
                    except Exception as e:
                        st.error("Failed to fetch route from backend.")
                        st.exception(e)

            # Render danh sách kèm ETA/Distance
            for i, p in enumerate(items, 1):
                name    = p.get("name", "Unknown")
                addr    = p.get("display_address", "")
                rating  = p.get("rating")
                reviews = p.get("user_ratings_total")

                sig      = p.get("signals") or {}
                raw_eta  = p.get("eta_s") or p.get("duration_s") or sig.get("duration_s")
                raw_dist = p.get("distance_m") or sig.get("distance_m")

                eta_txt  = _fmt_eta(raw_eta)
                dist_txt = _fmt_dist(raw_dist)

                meta = []
                if rating is not None:
                    meta.append(f"⭐ {rating}")
                    if reviews is not None:
                        meta.append(f"({reviews})")
                if eta_txt:
                    meta.append(f"🕒 {eta_txt}")
                if dist_txt:
                    meta.append(f"📏 {dist_txt}")

                st.markdown(
                    f"**{i}. {name}**  \n"
                    f"{addr}  \n"
                    f"{'  '.join(meta)}"
                )


# =========================
# CHAT BONG BÓNG (Messenger-like)
# =========================
# 1) Hai container làm "móc" cho CSS :has()
fab_box = st.container()
drawer_box = st.container()

# 2) Nút bong bóng
with fab_box:
    # hook để CSS nhận diện đúng block
    st.markdown('<span class="nf-fab-hook"></span>', unsafe_allow_html=True)
    if st.button("💬", key="nf_fab_btn", help="NestFeast Chat"):
        st.session_state.chat_open = not st.session_state.chat_open

# 3) Drawer nổi (panel chat)
if st.session_state.chat_open:
    with drawer_box:
        st.markdown('<span class="nf-drawer-hook"></span>', unsafe_allow_html=True)

        c1, c2, c3 = st.columns([5, 1, 1])
        with c1:
            st.subheader("NestFeast Chat")
            
        with c2:
            if st.button("✕", key="nf_close_btn"):
                st.session_state.chat_open = False
                st.stop()
        
        # ✅ Chỉ còn 1 nút Reset, dùng luôn key nf_reset_ctx
        with c3:
            if st.button("⚙️ Reset", key="nf_reset_ctx"):
                _nf_reset_chat_ctx()
                st.rerun()


        # (1) Nút bật/tắt debug
        # st.session_state.nf_debug_ctx = st.toggle(
        #    "🔎 Debug context",
        #    value=st.session_state.get("nf_debug_ctx", False),
        #)

        # (2) Expander soi context đang gửi sang BE
        #if st.session_state.nf_debug_ctx:
        #    with st.expander("🛠 Context debug", expanded=False):
        #        ctx = _current_chat_context()
        #        st.write({
        #            "ctx_confirmed":    st.session_state.get("ctx_confirmed"),
        #            "origin_selected":  st.session_state.get("origin_selected"),
        #            "origin_text_input":st.session_state.get("origin_text_input"),
        #            "chat_origin_text": st.session_state.get("chat_origin_text"),
        #            "origin_text(ctx)": ctx.get("origin_text"),
        #            "origin(ctx)":      ctx.get("origin"),
        #            "radius_km":        ctx.get("radius_km"),
        #            "mode":             ctx.get("mode"),
        #            "tags":             ctx.get("tags"),
        #            "__last_ctx_debug": st.session_state.get("__last_ctx_debug"),
        #        })
              
        # 3a) Onboarding khi thiếu context tối thiểu
        if not _context_ready():
            with st.chat_message("assistant"):
                st.markdown(_onboarding_message())

            # default: ưu tiên origin đã khai báo trong chat, nếu chưa thì dùng origin form trên
            chat_ori_default = (
                st.session_state.get("chat_origin_text")
                or st.session_state.get("origin_text_input", "")
            )
            # --- Safe push for nf_chat_origin (must run BEFORE the widget is created)
            _pending = st.session_state.pop("_nf_chat_origin_next", None)
            if isinstance(_pending, str):
                st.session_state["nf_chat_origin"] = _pending
            # ✅ CHỈ truyền `value=` nếu key chưa có trong session_state
            if "nf_chat_origin" in st.session_state:
                chat_ori = st.text_input("Origin (address)", key="nf_chat_origin")
            else:
                chat_ori = st.text_input("Origin (address)", value=chat_ori_default, key="nf_chat_origin")
            
            # 🔎 NEW — Autocomplete khi nhấn Enter (hoặc khi đủ ≥3 ký tự)
            _ac_sugs = []
            _q = (chat_ori or "").strip()
            if not st.session_state.get("origin_locked") and len(_q) >= 3:
                try:
                    with st.spinner("Suggesting..."):
                        _ac_sugs = origin_suggest_sync(_q)  # gọi BE để suggest
                except Exception:
                    _ac_sugs = []
            else:
                _ac_sugs = []

            # NEW — Hiển thị gợi ý + nút xác nhận origin
            _chosen = None
            if _ac_sugs:
                _labels = [
                    f"{i+1}. {s.get('main_text','')} — {s.get('secondary_text','')}"
                    for i, s in enumerate(_ac_sugs)
                ]
                _idx = st.radio(
                    "Suggestions:",
                    options=range(len(_labels)),
                    format_func=lambda i: _labels[i],
                    index=0,
                    key="nf_chat_onb_ac_idx",
                )
                if st.button("Confirm", key="nf_chat_onb_use_addr"):
                    _chosen = _ac_sugs[_idx]

            # NEW — tuỳ chọn: dùng vị trí hiện tại
            c_mylocA, c_mylocB = st.columns([1, 3])
            with c_mylocA:
                _use_loc = st.button("📍 Use my location", key="nf_chat_onb_use_loc")
            if _use_loc:
                lat, lng, acc, src = _get_my_location()
                if lat is None or lng is None:
                    st.warning("Couldn't get your location.")
                else:
                    resp = intake_origin(lat=lat, lng=lng)
                    ori_norm = (resp or {}).get("origin") or {}
                    display = ori_norm.get("display_address") or f"{lat:.6f}, {lng:.6f}"
                    st.session_state.origin_selected = {
                        "lat": float(lat), "lng": float(lng), "kind": "point",
                        "display_address": display, "place_id": ori_norm.get("place_id"),
                    }
                    st.session_state.origin_text = display
                    st.session_state.prev_origin_text = display
                    st.session_state.origin_locked = True
                    st.session_state._sync_origin_next = True
                    st.session_state.chat_origin_text = display
                    st.session_state._nf_chat_origin_next   = display
                    st.rerun()

                        
            # NEW — khi người dùng bấm “Confirm” cho 1 gợi ý
            if _chosen is not None:
                display = _chosen.get("description") or _chosen.get("main_text") or ""
                place_id = _chosen.get("place_id")
                lat, lng = pick_lat_lng(_chosen)

                # Nếu suggestion không có lat/lng → gọi intake_origin(origin_text=...)
                if not (_is_num(lat) and _is_num(lng)):
                    try:
                        norm = intake_origin(origin_text=display)
                        ori  = (norm or {}).get("origin") or {}
                        lat  = _to_float(ori.get("lat"))
                        lng  = _to_float(ori.get("lng"))
                        display  = ori.get("display_address") or display
                        place_id = ori.get("place_id") or place_id
                    except Exception:
                        pass

                # Vẫn không có toạ độ → đừng lock origin sai
                if not (_is_num(lat) and _is_num(lng)):
                    st.warning("This suggestion has no coordinates. Please pick another one or use 'Use my location'.")
                    st.stop()

                st.session_state.origin_selected = {
                    "lat": float(lat), "lng": float(lng), "kind": "point",
                    "display_address": display,
                    "place_id": place_id,
                }
                st.session_state.origin_text = display
                st.session_state.prev_origin_text = st.session_state.origin_text
                st.session_state.origin_locked = True
                st.session_state._sync_origin_next = True  # đẩy text đã chọn lên input
                st.session_state.chat_origin_text = st.session_state.origin_text
                st.session_state._nf_chat_origin_next   = st.session_state.origin_text  # ← PUSH vào widget Chat

                st.session_state.pop("nf_chat_onb_ac_idx", None)
                st.rerun()

                
            chat_radius = st.slider(
                "Radius (km)", 1, 7, int(st.session_state.get("radius_km", 3)),
                key="nf_chat_radius",
            )
            chat_mode = st.selectbox(
                "Transportation", list(MODE_MAP.keys()),
                index=list(MODE_MAP.keys()).index(st.session_state.get("mode_label", "motorcycling")),
                key="nf_chat_mode",
            )
            chat_opt  = st.selectbox(
                "Place type", list(TAG_MAP.keys()),
                index=list(TAG_MAP.keys()).index(st.session_state.get("opt_label", "Cafe")),
                key="nf_chat_opt",
            )

            colA, colB = st.columns([2,1])
            with colA:
                if st.button("Save & start", key="nf_save_begin"):
                    chosen = None

                    # 0) Nếu bạn đã Confirm trước đó (có lat/lng) thì thôi
                    if not st.session_state.get("origin_selected"):
                        # 1) Lấy query đang gõ
                        _q = (chat_ori or "").strip()

                        # 2) Gọi autocomplete để lấy candidates (chỉ 1 lần ở đây)
                        _ac_sugs = []
                        if len(_q) >= 3:
                            try:
                                _ac_sugs = origin_suggest_sync(_q) or []
                            except Exception:
                                _ac_sugs = []

                        # 3) Nếu đã click chọn radio trong list gợi ý → dùng index đó
                        idx = st.session_state.get("nf_chat_onb_ac_idx", None)
                        if isinstance(idx, int) and 0 <= idx < len(_ac_sugs):
                            chosen = _ac_sugs[idx]
                        # 4) Nếu chưa chọn nhưng chỉ có 1 gợi ý → dùng gợi ý duy nhất
                        elif len(_ac_sugs) == 1:
                            chosen = _ac_sugs[0]

                        # 5) Nếu đã có "chosen" → lock lat/lng để lần sau không bị hỏi lại
                        if chosen is not None:
                            display = chosen.get("description") or chosen.get("main_text") or _q
                            place_id = chosen.get("place_id")
                            lat, lng = pick_lat_lng(chosen)

                            # Nếu chưa có lat/lng → chuẩn hoá thêm lần nữa
                            if not (_is_num(lat) and _is_num(lng)):
                                try:
                                    norm = intake_origin(origin_text=display)
                                    ori  = (norm or {}).get("origin") or {}
                                    lat  = _to_float(ori.get("lat"))
                                    lng  = _to_float(ori.get("lng"))
                                    display  = ori.get("display_address") or display
                                    place_id = ori.get("place_id") or place_id
                                except Exception:
                                    pass

                            if not (_is_num(lat) and _is_num(lng)):
                                st.warning("This suggestion has no coordinates. Please pick the correct origin above or use 'Use my location'.")
                                st.stop()

                            st.session_state.origin_selected = {
                                "lat": float(lat), "lng": float(lng), "kind": "point",
                                "display_address": display,
                                "place_id": place_id,
                            }
                            st.session_state.origin_text = display
                            st.session_state.prev_origin_text = st.session_state.origin_text
                            st.session_state.origin_locked = True
                            st.session_state._sync_origin_next = True  # đẩy text đã lock lên ô input
                            st.session_state.chat_origin_text = st.session_state.origin_text
                            st.session_state._nf_chat_origin_next = st.session_state.origin_text
                            st.session_state.pop("nf_chat_onb_ac_idx", None)
                        else:
                            # Có nhiều gợi ý mà bạn chưa chọn → yêu cầu xác nhận, KHÔNG nhảy sang Quick actions
                            st.warning("Please pick the correct origin from the suggestions above before starting.")
                            st.stop()

                    # 6) Lưu các tham số khác + mở khóa chat
                    r = int(chat_radius)

                    st.session_state.chat_origin_text = (
                        st.session_state.get("origin_text") or chat_ori
                    )

                    # Core state dùng cho BE
                    st.session_state.radius_km = r
                    st.session_state.mode_label = chat_mode
                    st.session_state.opt_label  = chat_opt

                    # Báo cho main form lần rerun tới sẽ đẩy sang widget nf_main_*
                    st.session_state._nf_push_ctx_to_widgets = True

                    st.session_state.ctx_confirmed = True
                    st.session_state.chat_last_chips = []  # reset để hiện Quick actions
                    st.rerun()
            with colB:
                st.caption("Or pick the origin above and come back here.")

        # 3b) Đã đủ context → chat normal
        else:
            # Lịch sử chat
            for m in st.session_state.chat_msgs[-40:]:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])

            # Starter chips (Nearby + 3 toggle) – CHỈ hiện khi chưa có gợi ý từ backend
            starter = _starter_chips() if not st.session_state.chat_last_chips else []
            if starter:
                st.caption("Quick actions")
                st.markdown('<div class="nf-chip-row">', unsafe_allow_html=True)
                for i, chip in enumerate(starter):
                    if st.button(chip["label"], key=f"starter_{i}"):
                        if chip["intent"] == "ui_toggle":
                            menu = chip["payload"]["menu"]
                            st.session_state.nf_show_radius_menu = (menu == "radius")
                            st.session_state.nf_show_mode_menu   = (menu == "mode")
                            st.session_state.nf_show_origin_menu = (menu == "origin")
                            st.rerun()
                        else:
                            text = chip["payload"]["text"]
                            st.session_state.chat_msgs.append({"role": "user", "content": text})
                            resp = _send_or_prime(text)
                            _apply_ctx(resp)
                            st.session_state.chat_msgs.append({"role": "assistant", "content": resp.get("reply","")})
                            st.session_state.chat_last_chips = resp.get("chips") or []
                            st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

            # ----- Menu con: BÁN KÍNH -----
            if st.session_state.nf_show_radius_menu:
                r = st.slider(
                    "Choose radius (km)", 1, 7,
                    int(st.session_state.get("radius_km", 3)),
                    key="nf_radius_slider",
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Apply", key="nf_apply_radius"):
                        st.session_state.radius_km = r
                        text = f"radius {int(r)} km"
                        st.session_state.chat_msgs.append({"role": "user", "content": text})
                        resp = _send_or_prime(text)
                        _apply_ctx(resp)
                        st.session_state.chat_msgs.append({"role": "assistant", "content": resp.get("reply","")})
                        st.session_state.chat_last_chips = resp.get("chips") or []
                        st.session_state.nf_show_radius_menu = False
                        st.rerun()
                with c2:
                    if st.button("Close", key="nf_close_radius"):
                        st.session_state.nf_show_radius_menu = False
                        st.rerun()

            # ----- Menu con: PHƯƠNG TIỆN -----
            if st.session_state.nf_show_mode_menu:
                mode_now = st.session_state.get("mode_label", "motorcycling")
                m = st.selectbox(
                    "Choose transport", list(MODE_MAP.keys()),
                    index=list(MODE_MAP.keys()).index(mode_now),
                    key="nf_mode_select2",
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Apply", key="nf_apply_mode"):
                        st.session_state.mode_label = m
                        en = {"walking": "walk", "motorcycling": "motorcycle", "driving": "drive", "truck": "truck"}
                        text = f"I {en.get(m,m)}"
                        st.session_state.chat_msgs.append({"role": "user", "content": text})
                        resp = _send_or_prime(text)
                        _apply_ctx(resp)
                        st.session_state.chat_msgs.append({"role": "assistant", "content": resp.get("reply","")})
                        st.session_state.chat_last_chips = resp.get("chips") or []
                        st.session_state.nf_show_mode_menu = False
                        st.rerun()
                with c2:
                    if st.button("Close", key="nf_close_mode"):
                        st.session_state.nf_show_mode_menu = False
                        st.rerun()

            # ----- Menu con: ORIGIN -----
            if st.session_state.nf_show_origin_menu:
                ori_default = (
                    st.session_state.get("chat_origin_text")
                    or st.session_state.get("origin_text_input", "")
                )
                ori = st.text_input(
                    "New origin (address)",
                    value=ori_default,
                    key="nf_origin_inline",
                    help="Type an address and press Enter to see suggestions."
                )

                # 🔎 Autocomplete ngay khi gõ (>=3 ký tự)
                q = (ori or "").strip()
                if len(q) >= 3:
                    try:
                        sugs = origin_suggest_sync(q)  # sync call to backend
                    except Exception:
                        sugs = []
                else:
                    sugs = []

                # Hiển thị gợi ý & xác nhận (có gắn ⚠ nếu thiếu toạ độ)
                chosen = None
                if sugs:
                    labels = []
                    for i, s in enumerate(sugs):
                        lat_i, lng_i = pick_lat_lng(s)
                        base = f"{i+1}. {s.get('main_text','')} — {s.get('secondary_text','')}"
                        if not (_is_num(lat_i) and _is_num(lng_i)):
                            base += "  ⚠ (no coordinates)"
                        labels.append(base)

                    idx = st.radio(
                        "Suggestions:",
                        options=range(len(labels)),
                        format_func=lambda i: labels[i],
                        index=0,
                        key="nf_chat_ac_idx",
                    )
                    if st.button("Confirm", key="nf_use_origin_inline"):
                        chosen = sugs[idx]

                # 🎯 “Use my location”
                c1, c2, c3 = st.columns(3)
                with c1:
                    use_loc = st.button("📍 Use my location", key="nf_chat_use_myloc")
                with c2:
                    save_only = st.button("Save text only", key="nf_change_origin_inline")
                with c3:
                    close_menu = st.button("Close", key="nf_close_origin")

                if use_loc:
                    lat, lng, acc, src = _get_my_location()
                    if lat is None or lng is None:
                        st.warning("Couldn't get your location.")
                    else:
                        resp = intake_origin(lat=lat, lng=lng)
                        ori_norm = (resp or {}).get("origin") or {}
                        display = ori_norm.get("display_address") or f"{lat:.6f}, {lng:.6f}"

                        st.session_state.origin_selected = {
                            "lat": float(lat), "lng": float(lng), "kind": "point",
                            "display_address": display, "place_id": ori_norm.get("place_id"),
                        }
                        st.session_state.origin_text = display
                        st.session_state.prev_origin_text = display
                        st.session_state.origin_locked = True
                        st.session_state.nf_show_origin_menu = False
                        st.session_state.chat_last_chips = []
                        st.session_state._sync_origin_next = True
                        st.session_state.chat_origin_text = display
                        st.session_state._nf_chat_origin_next = display
                        st.session_state.ctx_confirmed = True
                        # reset kết quả cũ
                        st.session_state.res = None
                        st.session_state.pending_cands = None
                        st.session_state.route_preview = None
                        st.rerun()

                # ✅ Xác nhận 1 địa chỉ từ autocomplete
                if chosen is not None:
                    display = (
                        chosen.get("description")
                        or chosen.get("main_text")
                        or q
                    )
                    place_id = chosen.get("place_id")
                    lat, lng = pick_lat_lng(chosen)

                    # Nếu suggestion thiếu lat/lng → chuẩn hoá qua intake_origin
                    if not (_is_num(lat) and _is_num(lng)):
                        try:
                            norm = intake_origin(origin_text=display)
                            ori_norm = (norm or {}).get("origin") or {}
                            lat = _to_float(ori_norm.get("lat"))
                            lng = _to_float(ori_norm.get("lng"))
                            display = ori_norm.get("display_address") or display
                            place_id = ori_norm.get("place_id") or place_id
                        except Exception:
                            pass

                    # Vẫn không có toạ độ → CẢNH BÁO, không lock origin
                    if not (_is_num(lat) and _is_num(lng)):
                        st.warning(
                            "This suggestion has no coordinates. "
                            "Please pick another one or use 'Use my location'."
                        )
                        st.stop()

                    st.session_state.origin_selected = {
                        "lat": float(lat), "lng": float(lng), "kind": "point",
                        "display_address": display,
                        "place_id": place_id,
                    }
                    st.session_state.origin_text = display
                    st.session_state.prev_origin_text = st.session_state.origin_text
                    st.session_state.chat_origin_text = st.session_state.origin_text
                    st.session_state.origin_locked = True
                    st.session_state.ctx_confirmed = True
                    st.session_state.nf_show_origin_menu = False
                    st.session_state.chat_last_chips = []  # reset chip cũ
                    st.session_state._sync_origin_next = True
                    # reset kết quả cũ
                    st.session_state.res = None
                    st.session_state.pending_cands = None
                    st.session_state.route_preview = None
                    st.rerun()

                if save_only:
                    # chỉ lưu text (không lock) – giữ behaviour cũ
                    st.session_state.chat_origin_text = ori
                    st.session_state.nf_show_origin_menu = False
                    st.session_state.chat_last_chips = []
                    st.rerun()

                if close_menu:
                    st.session_state.nf_show_origin_menu = False
                    st.rerun()


            # Chips do backend trả về
            if st.session_state.chat_last_chips:
                st.caption("More suggestions")
                st.markdown('<div class="nf-chip-row">', unsafe_allow_html=True)
                for i, chip in enumerate(st.session_state.chat_last_chips):
                    label = chip.get("label") or "·"
                    if st.button(label, key=f"chip_{i}"):
                        pl = chip.get("payload") or {}
                        it = (chip or {}).get("intent")
                        
                        o = pl.get("origin") if isinstance(pl, dict) else None
                        if isinstance(o, dict) and _is_num(o.get("lat")) and _is_num(o.get("lng")):
                            st.session_state.origin_selected = {
                                "lat": float(o["lat"]), "lng": float(o["lng"]), "kind": "point",
                                "display_address": o.get("display_address"),
                                "place_id": o.get("place_id"),
                            }
                            st.session_state.origin_text = (
                                o.get("display_address") or st.session_state.get("origin_text") or ""
                            )
                            st.session_state.prev_origin_text = st.session_state.origin_text
                            st.session_state.origin_locked = True
                            st.session_state.ctx_confirmed = True
                            st.session_state._sync_origin_next = True
                            st.session_state.chat_origin_text = st.session_state.origin_text
                            st.session_state._nf_chat_origin_next = st.session_state.origin_text
                            # Xoá chip cũ để tránh lặp lại hỏi confirm
                            st.session_state.chat_last_chips = []
                            st.rerun()
                        
                        if it == "change_radius" and "radius_km" in pl:
                            try:
                                r = int(float(pl["radius_km"]))
                                st.session_state.radius_km = r
                            except Exception:
                                pass

                        if it == "change_mode" and "mode" in pl:
                            m = str(pl["mode"])
                            st.session_state.mode_label = m

                        # text gửi BE: ưu tiên payload.text (tuyệt đối), fallback _chip_to_prompt/label
                        send_text = pl.get("text") or _chip_to_prompt(chip, "en") or chip.get("text") or (chip.get("label") or "")
                        show_text = chip.get("label") or "·"
                        st.session_state.chat_msgs.append({"role": "user", "content": show_text})
                        resp = _send_or_prime(send_text)
                        _apply_ctx(resp)
                        st.session_state.chat_msgs.append({"role": "assistant", "content": resp.get("reply","")})
                        st.session_state.chat_last_chips = resp.get("chips") or []
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

            
            # Gợi ý placeholder chỉ còn "chi tiết <name> …"
            try:
                _items_for_ph = (st.session_state.get("res") or {}).get("items") or []
                _ex_name = next((p.get("name") for p in _items_for_ph if p.get("name")), None)
            except Exception:
                _ex_name = None
            nf_placeholder = f"e.g., details of {_ex_name} …" if _ex_name else "e.g., Details of [place name] …"

            # Ô nhập tin nhắn
            with st.form("nf_chat_form", clear_on_submit=True):
                c_inp, c_btn = st.columns([4, 1])
                with c_inp:
                    nf_text = st.text_input(
                        "Type a message…",
                        key="nf_chat_text",
                        placeholder=nf_placeholder,
                        label_visibility="collapsed",
                    )
                with c_btn:
                    nf_send = st.form_submit_button("➤")

            if 'nf_send_guard' not in st.session_state:
                st.session_state.nf_send_guard = 0

            if nf_send and nf_text.strip() and st.session_state.nf_send_guard == 0:
                st.session_state.nf_send_guard = 1
                msg = nf_text.strip()
                st.session_state.chat_msgs.append({"role": "user", "content": msg})
                resp = _send_or_prime(msg)
                _apply_ctx(resp)
                st.session_state.chat_msgs.append({"role": "assistant", "content": resp.get("reply", "")})
                st.session_state.chat_last_chips = resp.get("chips") or []
                st.session_state.nf_send_guard = 0
                st.rerun()

# 4) CSS – cố định vị trí 2 container bằng :has()
st.markdown("""
<style>
/* FAB (nút tròn) – đặt cố định góc phải dưới */
section.main .block-container div:has(> .element-container .nf-fab-hook) button {
  position: fixed; right: 22px; bottom: 22px; z-index: 9999;
  width: 56px; height: 56px; border-radius: 50%;
  background:#2F4F3A; color:#fff; font-size:22px; font-weight:700;
  box-shadow:0 8px 24px rgba(0,0,0,.18); border: none;
}

/* Drawer nổi – cố định phía trên FAB */
section.main .block-container div:has(> .element-container .nf-drawer-hook) {
  position: fixed; right: 22px; bottom: 86px; z-index: 9998;
  width: 380px; max-height: 70vh; overflow: auto;
  background: #fff; border: 1px solid #e5e5e5; border-radius: 14px;
  box-shadow:0 10px 28px rgba(0,0,0,.15); padding: 10px 12px;
}

/* trang trí nhỏ */
.nf-chip-row { display:flex; flex-wrap:wrap; gap:10px; margin:8px 4px; }
section.main .block-container div:has(> .element-container .nf-drawer-hook) .stTextInput > div > div > input{
  border-radius: 12px; padding: 10px 12px; border: 1px solid #ddd;
}
section.main .block-container div:has(> .element-container .nf-drawer-hook) .stForm .stButton > button{
  height: 42px; width: 100%; border-radius: 12px; background: #2F4F3A; color: #fff;
}
</style>
""", unsafe_allow_html=True)
