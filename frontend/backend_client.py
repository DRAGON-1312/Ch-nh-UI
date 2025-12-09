from __future__ import annotations
import os, requests, time, threading
import streamlit as st
import httpx
import asyncio
from collections import OrderedDict

BASE = (st.secrets.get("backend", {}).get("base")
        or os.getenv("BACKEND_BASE", "http://127.0.0.1:8000")).rstrip("/")
TIMEOUT = int(st.secrets.get("backend", {}).get("timeout_s", 60))

_AUTOCOMPLETE_CACHE_TTL = 60  # giây
_AUTOCOMPLETE_CACHE_MAX = 256
_AUTOCOMPLETE_CACHE: "OrderedDict[str, tuple[float, list]]" = OrderedDict()

def _auto_key(q: str, lat: float | None, lng: float | None, limit: int) -> str:
    return f"{q.strip().lower()}|{lat or ''}|{lng or ''}|{limit}"

def _cache_get(key: str):
    now = time.time()
    item = _AUTOCOMPLETE_CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if now - ts > _AUTOCOMPLETE_CACHE_TTL:
        # hết hạn
        _AUTOCOMPLETE_CACHE.pop(key, None)
        return None
    # move-to-end (LRU)
    _AUTOCOMPLETE_CACHE.move_to_end(key)
    return val

def _cache_put(key: str, value: list):
    if len(_AUTOCOMPLETE_CACHE) >= _AUTOCOMPLETE_CACHE_MAX:
        _AUTOCOMPLETE_CACHE.popitem(last=False)  # pop oldest (LRU)
    _AUTOCOMPLETE_CACHE[key] = (time.time(), value)

def _run_coro_in_new_loop(coro):
    result, error = [], []
    def runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result.append(loop.run_until_complete(coro))
        except Exception as e:
            error.append(e)
        finally:
            try:
                loop.close()
            except Exception:
                pass
    t = threading.Thread(target=runner, daemon=True)
    t.start(); t.join()
    if error:
        raise error[0]
    return result[0]

def origin_suggest_sync(q: str, lat: float | None = None, lng: float | None = None, limit: int = 5):
    """
    Synchronous wrapper cho Streamlit:
    - Dùng cache LRU+TTL để giảm call.
    - Tự xử lý khi đang có event-loop (không dùng asyncio.run() trực tiếp).
    """
    key = _auto_key(q, lat, lng, limit)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        # có loop đang chạy?
        try:
            loop = asyncio.get_running_loop()
            running = loop.is_running()
        except RuntimeError:
            running = False

        if running:
            data = _run_coro_in_new_loop(origin_suggest(q=q, lat=lat, lng=lng, limit=limit))
        else:
            data = asyncio.run(origin_suggest(q=q, lat=lat, lng=lng, limit=limit))
    except Exception:
        # degrade gracefully
        data = []

    _cache_put(key, data)
    return data


def _post(path: str, body: dict, *, timeout: int | None = None, retries: int = 2):
    url = BASE.rstrip("/") + path
    to = timeout or TIMEOUT or 30  # 30s vừa phải hơn 60s
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=body, timeout=to)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            # 4xx không retry, 5xx/timeout mới retry
            status = getattr(e.response, "status_code", None)
            if status and 400 <= status < 500:
                break
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    # cố gắng bóc nội dung lỗi JSON (nếu có) để thông báo gọn
    detail = None
    try:
        if hasattr(last_err, "response") and last_err.response is not None:
            detail = last_err.response.json()
    except Exception:
        detail = getattr(last_err, "response", None) and last_err.response.text
    raise RuntimeError(f"Backend POST {path} failed: {detail or last_err}")


def intake_origin(origin_text=None, lat=None, lng=None, **kw):
    body = {k: v for k, v in {
        "origin_text": origin_text,
        "lat": lat, "lng": lng,
        **kw
    }.items() if v is not None}
    return _post("/intake/origin", body)  # res: {origin, reason_code, need_confirm, candidates}


def search_nearby(origin=None, origin_text=None, lat=None, lng=None, **kw):
    body = {k: v for k, v in {
        "origin": origin, "origin_text": origin_text, "lat": lat, "lng": lng, **kw
    }.items() if v is not None}
    return _post("/search/nearby", body)  # res: {need_confirm, origin, items|candidates}


def search_ranked(origin=None, origin_text=None, lat=None, lng=None, **kw):
    body = {k: v for k, v in {
        "origin": origin, "origin_text": origin_text, "lat": lat, "lng": lng, **kw
    }.items() if v is not None}
    return _post("/search/ranked", body)  # res: {need_confirm, origin, items|candidates}


def route_preview(origin, dest, mode="motorcycling", lang="vi"):
    return _post("/route/preview", {
        "origin": origin, "dest": dest, "mode": mode, "lang": lang
    })


# ===== Autocomplete (async + sync + LRU cache nhỏ) =====

# cache theo key (q, lat, lng, limit), sống theo vòng đời process
_AUTOCOMPLETE_CACHE: dict[str, list] = {}
_AUTOCOMPLETE_CACHE_MAX = 256


def _auto_key(q: str, lat: float | None, lng: float | None, limit: int) -> str:
    return f"{q.strip().lower()}|{lat or ''}|{lng or ''}|{limit}"


async def origin_suggest(q: str, lat: float | None = None, lng: float | None = None, limit: int = 5):
    params = {"q": q, "limit": limit}
    if lat is not None and lng is not None:
        params["lat"] = lat
        params["lng"] = lng

    async with httpx.AsyncClient(base_url=BASE, timeout=5.0) as client:
        r = await client.get("/autocomplete/origin-suggest", params=params)
        r.raise_for_status()
        return r.json()  # list các suggestion


def origin_suggest_sync(q: str, lat: float | None = None, lng: float | None = None, limit: int = 5):
    """
    Synchronous wrapper cho Streamlit + cache đơn giản để giảm số lần hit backend.
    """
    key = _auto_key(q, lat, lng, limit)
    cached = _AUTOCOMPLETE_CACHE.get(key)
    if cached is not None:
        return cached

    data = asyncio.run(origin_suggest(q=q, lat=lat, lng=lng, limit=limit))

    # LRU siêu đơn giản: nếu quá nhiều key thì clear cả cache
    if len(_AUTOCOMPLETE_CACHE) >= _AUTOCOMPLETE_CACHE_MAX:
        _AUTOCOMPLETE_CACHE.clear()
    _AUTOCOMPLETE_CACHE[key] = data
    return data


# ===== Chatbot (chatbot.py services → /chatbot/ask) =====
def chatbot_ask(message: str,
                history: list[dict] | None = None,
                context: dict | None = None) -> dict:
    """
    Gọi router /chatbot/ask.

    Parameters
    ----------
    message : str
        Câu người dùng (ví dụ: "cafe gần đây", "xem #2", "chi tiết Soo Kafe", "chi tiết #2", ...)
    history : list[dict]
        Lịch sử hội thoại dạng [{"role":"user"|"assistant", "content":"..."}]. Có thể để [].
    context : dict
        Ngữ cảnh: {"origin": {...}, "tags": ["cafe"|...], "mode": "motorcycling"|...,
                   "radius_km": 2.0, "lang": "vi"|"en", ...}

    Returns
    -------
    dict : ChatbotResponse {reply, intent, chips, context}
    """
    body = {
        "message": message,
        "history": history or [],
        "context": context or {}
    }
    return _post("/chatbot/ask", body)


# ---- Convenience wrappers (tuỳ chọn) ----
def chatbot_nearby(tags: list[str],
                   origin: dict,
                   radius_km: float = 2.0,
                   mode: str = "motorcycling",
                   lang: str = "vi") -> dict:
    """Gợi ý gần đây theo tag."""
    ctx = {"origin": origin, "radius_km": radius_km, "mode": mode, "tags": tags, "lang": lang}
    msg = "cafe gần đây" if ("cafe" in tags and lang == "vi") else \
          "restaurants near me" if ("restaurant" in tags and lang == "en") else \
          "nearby"
    return chatbot_ask(message=msg, history=[], context=ctx)


def chatbot_pick(index: int, context: dict) -> dict:
    """Chọn mục #index trong danh sách hiện tại."""
    return chatbot_ask(message=f"xem #{index}", history=[], context=context)


def chatbot_detail_index(index: int, context: dict, lang: str = "vi") -> dict:
    """Xem chi tiết mục #index (gọi Gemini trong backend)."""
    msg = f"chi tiết #{index}" if lang == "vi" else f"details #{index}"
    return chatbot_ask(message=msg, history=[], context=context)


def chatbot_detail_name(name: str, tags: list[str], origin: dict | None = None,
                        mode: str = "motorcycling", lang: str = "vi") -> dict:
    """Xem chi tiết theo tên địa điểm."""
    msg = f"chi tiết {name}" if lang == "vi" else f"details for {name}"
    ctx = {"tags": tags, "mode": mode, "lang": lang}
    if origin:
        ctx["origin"] = origin
    return chatbot_ask(message=msg, history=[], context=ctx)


def chatbot_change_radius(delta_km: float, context: dict, lang: str = "vi") -> dict:
    """Thay đổi bán kính (ví dụ +2 km)."""
    msg = f"tăng bán kính {delta_km} km" if lang == "vi" else f"increase radius {delta_km} km"
    # context.radius_km sẽ được backend cập nhật lại
    return chatbot_ask(message=msg, history=[], context=context)


def chatbot_change_mode(mode: str, context: dict, lang: str = "vi") -> dict:
    """Đổi chế độ di chuyển: walking|motorcycling|driving."""
    vi = {"walking": "đi bộ", "motorcycling": "xe máy", "driving": "ô tô"}
    en = {"walking": "walk", "motorcycling": "motorcycle", "driving": "drive"}
    msg = f"tôi {vi.get(mode, mode)}" if lang == "vi" else en.get(mode, mode)
    return chatbot_ask(message=msg, history=[], context=context)


# --- IP-based rough geolocation (fallback) ---
def ip_location() -> dict:
    """
    Lấy vị trí gần đúng từ public IP. 
    Return: {"lat": float, "lng": float}
    """
    # Ưu tiên ipapi.co
    try:
        r = requests.get("https://ipapi.co/json/", timeout=4)
        if r.ok:
            j = r.json()
            lat = j.get("latitude") or j.get("lat")
            lng = j.get("longitude") or j.get("lon")
            if lat is not None and lng is not None:
                return {"lat": float(lat), "lng": float(lng)}
    except Exception:
        pass

    # Fallback ipinfo.io
    try:
        r = requests.get("https://ipinfo.io/json", timeout=4)
        if r.ok:
            j = r.json()
            loc = (j.get("loc") or "").split(",")
            if len(loc) == 2:
                return {"lat": float(loc[0]), "lng": float(loc[1])}
    except Exception:
        pass

    raise RuntimeError("Could not get IP-based location")