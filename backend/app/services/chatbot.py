from __future__ import annotations

import re, os
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.chatbot import (
    ChatbotRequest,
    ChatbotResponse,
    ChatbotIntent,
    ChatbotContext,
    ChatChip,
    PlaceDetailSummary,
)

from app.services.ranked import search_ranked
from app.providers.llm.gemini import ChatMessage, chat as gemini_chat
from app.services.autocomplete import suggest_places_as_dict
import logging
import json
import unicodedata
import hashlib

log = logging.getLogger("uvicorn.error")  # dùng chính logger của Uvicorn
def _ctx_dbg(ctx):
    try:
        return {
            "has_origin": bool(getattr(ctx, "origin", None)),
            "origin_text": getattr(ctx, "origin_text", None),
            "radius_km": float(getattr(ctx, "radius_km", 0) or 0),
            "mode": getattr(ctx, "mode", None),
            "tags": [str(t) for t in (getattr(ctx, "tags", []) or [])],
            "active_n": len(getattr(ctx, "active_place_ids", []) or []),
        }
    except Exception:
        return {"_": "ctx_dbg_failed"}
    

_DEF_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ========== Helpers (nhẹ & gọn) ==========
def _pick_lat_lng(obj: Any) -> Tuple[Optional[float], Optional[float]]:
    """
    Lấy (lat, lng) từ dict hoặc object:
    - Ưu tiên key trực tiếp: lat/lng
    - Sau đó tới location/geometry.{lat,lng}/{latitude,longitude}/{x,y}
    """
    lat = lng = None

    if isinstance(obj, dict):
        loc = (
            obj.get("location")
            or obj.get("loc")
            or obj.get("geometry")
            or {}
        )
        lat = obj.get("lat") or loc.get("lat") or loc.get("latitude") or loc.get("y")
        lng = obj.get("lng") or loc.get("lng") or loc.get("lon") or loc.get("longitude") or loc.get("x")
    else:
        # phòng trường hợp trả về object
        lat = getattr(obj, "lat", None) or getattr(obj, "latitude", None)
        lng = getattr(obj, "lng", None) or getattr(obj, "lon", None) or getattr(obj, "longitude", None)

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    return _to_float(lat), _to_float(lng)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
def _strip_code_fence(s: str) -> str:
    return _FENCE_RE.sub("", s or "")

def _is_vi(text: str) -> bool:
    return bool(re.search(r"[À-ỹ]", text or ""))

def _lang(text: str) -> str:
    return "vi" if _is_vi(text) else "en"

def _km(n: Optional[float]) -> str:
    return "–" if n is None else f"{n:g} km"

def _mins(n: Optional[float]) -> str:
    return "–" if n is None else f"{int(round(n))} min"

def _rating(r: Optional[float], c: Optional[int]) -> str:
    if r is None and c is None:
        return "–"
    if r is None:
        return f"{c or 0} rev"
    if c is None:
        return f"{r:.1f}★"
    return f"{r:.1f}★ ({c})"

def _extract_radius_km(msg: str) -> Optional[float]:
    s = (msg or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*km", s)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    m2 = re.search(r"\bradius\s*[:=]?\s*(\d+(?:\.\d+)?)\b", s)
    if m2:
        try:
            return float(m2.group(1))
        except Exception:
            return None
    return None

def _extract_mode(msg: str) -> Optional[str]:
    s = (msg or "").lower()
    if any(k in s for k in ["đi bộ", "walking", "walk"]):
        return "walking"
    if any(k in s for k in ["xe máy", "motor", "motorbike", "motorcycle", "motorcycling"]):
        return "motorcycling"
    if any(k in s for k in ["ô tô", "oto", "car", "driving", "drive"]):
        return "driving"
    if any(k in s for k in ["xe tải", "xe tai", "truck"]):
        return "truck"
    return None

def _extract_pick_index(msg: str) -> Optional[int]:
    m = re.search(r"(?:chọn|chon|xem|pick|#)\s*#?\s*(\d+)", (msg or "").lower())
    if m:
        try:
            i = int(m.group(1))
            return i if i >= 1 else None
        except Exception:
            return None
    return None

def _extract_detail_name(msg: str) -> Optional[str]:
    """
    Tách tên khi người dùng gõ kiểu: 'chi tiết Soo Kafe', 'thông tin về Đồng Cafe'.
    """
    s = (msg or "").strip().lower()
    m = re.search(r"(?:chi tiết|thông tin|details?|info)\s+(?:về|ve|của|cua)?\s*(.+)", s)
    if m:
        name = m.group(1).strip().strip("'\"")
        # nếu người dùng đã nói '#2' thì coi như index, không lấy name
        if not re.search(r"#?\d+$", name):
            return name
    return None


async def _origin_candidates_via_autocomplete(
    origin_text: str,
    ctx: ChatbotContext,
    lang: str,
) -> Optional[ChatbotResponse]:
    """
    Dùng TrackAsia autocomplete để gợi ý địa chỉ xuất phát.
    Trả về ChatbotResponse (intent=CLARIFY_ORIGIN) nếu có gợi ý; None nếu không.
    """
    q = (origin_text or "").strip()
    if not q:
        return None

    # có thể bias theo vị trí đã biết (nếu có)
    center = None
    try:
        if getattr(ctx, "origin", None):
            o = ctx.origin
            center = (float(getattr(o, "lat", None)), float(getattr(o, "lng", None)))
            if any(v is None for v in center):
                center = None
    except Exception:
        center = None

    try:
        suggs = await suggest_places_as_dict(
            query=q,
            center=center,
            limit=5,
            new_admin=True,
            include_old_admin=False,
        )
    except Exception:
        suggs = []

    if not suggs:
        return None

    chips: List[ChatChip] = []
    for s in suggs[:5]:
        label = s.get("main_text") or s.get("description") or "Pick this address"
        sec   = s.get("secondary_text") or ""
        if sec:
            label = f"{label} — {sec}"

        lat, lng = _pick_lat_lng(s)
        
        # ⚠️ BỎ QUA những suggestion không lấy được toạ độ
        if lat is None or lng is None:
            continue  # tránh gửi origin NULL xuống FE

        chips.append(ChatChip(
            label=label,
            intent=ChatbotIntent.QUICK_RECOMMEND,  # FE sẽ set ctx.origin rồi gọi lại
            payload={
                "origin": {
                    "lat": lat,
                    "lng": lng,
                    "kind": "point",
                    "place_id": s.get("place_id"),
                    "display_address": s.get("description") or s.get("main_text"),
                    "source": "autocomplete",
                    "confidence": 0.0,
                }
            },
        ))

    note = (
        "Mình tìm được vài vị trí, bạn chọn đúng địa chỉ xuất phát nhé."
        if lang == "vi"
        else "I found a few possible starting points—please pick the correct one."
    )
    return ChatbotResponse(
        reply=note,
        intent=ChatbotIntent.CLARIFY_ORIGIN,
        chips=chips,
        context=ctx,
    )


def _detect_intent(message: str, ctx: ChatbotContext) -> Tuple[ChatbotIntent, Dict[str, Any]]:
    s = (message or "").lower()

    # --- 1) Đổi mode ---
    mode = _extract_mode(s)
    if mode:
        return ChatbotIntent.CHANGE_MODE, {"mode": mode}

    # --- 2) Đổi bán kính ---
    if any(k in s for k in ["radius", "bán kính", "ban kinh", "km"]):
        r = _extract_radius_km(s)
        if r:
            return ChatbotIntent.CHANGE_RADIUS, {"radius_km": r}

    # --- 3) Chi tiết / thông tin (đưa lên trước pick) ---
    if any(k in s for k in ["chi tiết", "thông tin", "details", "detail", "info"]):
        return ChatbotIntent.DETAIL_PLACE, {
            "index": _extract_pick_index(s),   # 'chi tiết #2'
            "name": _extract_detail_name(s),   # 'chi tiết Soo Kafe'
        }

    # --- 4) pick #n ---
    pick = _extract_pick_index(s)
    if pick:
        return ChatbotIntent.PICK_PLACE, {"index": pick}

    # --- 5) Nhận diện các câu gợi ý gần đây / quanh đây / theo ngành ---
    REC_WORDS = [
        "gợi ý", "goi y", "tìm", "tim",
        "nearby", "around me", "near me",
        "quanh đây", "quanh day",
        "gần đây", "gan day", "gần", "gan",
    ]
    CAT_WORDS = [
        "quán", "quan", "cafe", "cà phê", "ca phe", "coffee",
        "nhà hàng", "nha hang", "restaurant",
        "khách sạn", "khach san", "hotel",
    ]
    has_rec = any(k in s for k in REC_WORDS)
    has_cat = any(k in s for k in CAT_WORDS)

    # Khi CHƯA có origin & origin_text -> yêu cầu làm rõ origin
    if (ctx.origin is None) and (not getattr(ctx, "origin_text", None)) and (has_rec or has_cat):
        return ChatbotIntent.CLARIFY_ORIGIN, {}

    # Còn lại, nếu có tín hiệu "gần đây/khách sạn/…" thì coi là recommend nhanh
    if has_rec or has_cat:
        return ChatbotIntent.QUICK_RECOMMEND, {}

    # --- 6) Mặc định: trả về QUICK_RECOMMEND để không rơi vào None ---
    return ChatbotIntent.QUICK_RECOMMEND, {}


def _chips_basic(ctx: ChatbotContext, lang: str) -> List[ChatChip]:
    chips: List[ChatChip] = []

    # --- Radius adjustments relative to the *confirmed* radius ---
    r0 = float(getattr(ctx, "radius_km", 3.0) or 3.0)
    MIN_R, MAX_R = 1.0, 7.0
    
    def _clamp(x: float) -> int:
        try:
            return int(max(MIN_R, min(MAX_R, float(x))))
        except Exception:
            return int(r0)

    deltas = (-2, -1, 1, 2)
    target_rs: list[int] = []

    for d in deltas:
        new_r = _clamp(r0 + d)
        # bỏ qua nếu sau khi clamp vẫn bằng radius hiện tại
        if new_r == _clamp(r0):
            continue
        if new_r not in target_rs:
            target_rs.append(new_r)

    seen: set[int] = set()
    for new_r in target_rs:
        if new_r in seen:
            continue
        seen.add(new_r)

        # delta THỰC SỰ sau khi clamp
        d = new_r - int(_clamp(r0))
        if d == 0:
            continue
        step = abs(d)

        if lang == "vi":
            label = (
                f"Giảm bán kính {step} km" if d < 0
                else f"Tăng bán kính {step} km"
            )
            text = f"bán kính {new_r} km"
        else:
            label = (
                f"Decrease radius {step} km" if d < 0
                else f"Increase radius {step} km"
            )
            text = f"radius {new_r} km"

        chips.append(ChatChip(
            label=label,
            intent=ChatbotIntent.CHANGE_RADIUS,
            payload={"radius_km": new_r, "text": text},
        ))

    # --- Travel mode quick switches (now includes truck) ---
    modes = [
        ("walking",      "Đi bộ",  "Walk"),
        ("motorcycling", "Xe máy", "Motorcycle"),
        ("driving",      "Ô tô",   "Car"),
        ("truck",        "Xe tải", "Truck"),
    ]
    for m, vi, en in modes:
        chips.append(ChatChip(
            label=(vi if lang == "vi" else en),          # <-- sửa dòng này
            intent=ChatbotIntent.CHANGE_MODE,
            payload={
                "mode": m,
                "text": (f"tôi {vi.lower()}" if lang == "vi" else en.lower())  # giữ prompt gửi BE
            },
        ))
        
    log.info(
        "chips.debug.radius_candidates=%s modes=%s",
        [c.payload.get("radius_km") for c in chips if c.intent == ChatbotIntent.CHANGE_RADIUS],
        [c.payload.get("mode") for c in chips if c.intent == ChatbotIntent.CHANGE_MODE],
    )
    return chips


def _coerce_place_dict(x: Any) -> Dict[str, Any]:
    """Chuẩn hoá một candidate từ ranked thành dict thống nhất:
       name, address, place_id, location{lat,lng}, signals{eta_s,distance_m}, rating, user_ratings_total, tags.
       Chấp nhận nhiều alias key từ các provider khác nhau.
    """
    def g(obj, *names):
        for n in names:
            if obj is None:
                return None
            if isinstance(obj, dict) and n in obj and obj[n] not in (None, ""):
                return obj[n]
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v not in (None, ""):
                    return v
        return None

    # phần tử chính có thể nằm ở x.place / x.doc / x.data hoặc ngay trên x
    place = g(x, "place", "doc", "data") or x
    signals = g(x, "signals", "_signals") or {}
    loc     = g(place, "location", "loc", "geometry") or {}

    lat = g(loc, "lat", "latitude", "y")
    lng = g(loc, "lng", "lon", "longitude", "x")

    name = g(place, "name", "title", "place_name", "display_name")
    address = g(place, "address", "formatted_address", "display_address", "vicinity")

    # rating / reviews
    rating = g(place, "rating", "rating_value", "score")
    reviews = g(place, "user_ratings_total", "reviews_count", "user_ratings", "rating_count")

    # tags / categories
    tags = g(place, "tags", "types", "categories", "serp_types") or []
    if isinstance(tags, str):
        tags = [tags]

    # place_id có thể là id / place_id / gid / google_place_id / reference / data_id …
    place_id = (
        g(place, "place_id", "id", "gid", "google_place_id", "google_id", "reference", "data_id")
        or g(x, "place_id", "id", "gid")
    )

    # ETA / distance có thể nằm trong signals hoặc ở chỗ khác; convert sang số nếu là string
    eta_s = g(signals, "eta_s", "eta", "duration_s", "duration")
    dist_m = g(signals, "distance_m", "distance", "dist_m")

    def _to_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        try:
            # "22 min" -> phút -> giây
            if s.endswith("min"):
                return float(s.split()[0]) * 60.0
            if s.endswith("s"):
                return float(s[:-1])
            if s.endswith("km"):
                return float(s.split()[0]) * 1000.0
            if s.endswith("m"):
                return float(s[:-1])
            return float(s)
        except Exception:
            return None

    eta_s  = _to_float(eta_s)
    dist_m = _to_float(dist_m)

    # nếu provider không trả id, tạo id ổn định từ (name, lat, lng)
    if not place_id:
        key = f"{name or ''}|{lat or ''}|{lng or ''}"
        if key.strip("|"):
            place_id = "nf_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]

    return {
        "name": name,
        "address": address,
        "place_id": place_id,
        "location": {"lat": lat, "lng": lng},
        # giữ signals gốc
        "signals": {"eta_s": eta_s, "distance_m": dist_m},
        # thêm 2 khóa phẳng để code hiện có đọc được
        "eta_s": eta_s,
        "distance_m": dist_m,
        "rating": rating,
        "user_ratings_total": reviews,
        "tags": tags,
    }


def _format_top3_reply(places: List[Dict[str, Any]], lang: str) -> str:
    if not places:
        return "Không tìm thấy kết quả phù hợp." if lang == "vi" else "No suitable results were found."
    lines = []
    for i, p in enumerate(places[:3], 1):
        name = p.get("name") or "—"
        rating = _rating(p.get("rating"), p.get("user_ratings_total"))
        dist_km = round(float(p["distance_m"]) / 1000.0, 2) if p.get("distance_m") is not None else None
        eta_min = float(p["eta_s"]) / 60.0 if p.get("eta_s") is not None else None
        tail = f"{_mins(eta_min)}, {_km(dist_km)} • {rating}"
        lines.append(f"{i}. {name} — {tail}")
    header = "Top lựa chọn gần bạn:" if lang == "vi" else "Top nearby picks:"
    return header + "\n" + "\n".join(lines)


def _get_origin_text(req: "ChatbotRequest", ctx: "ChatbotContext") -> Optional[str]:
    """
    Trích origin_text một cách 'cứng đầu' nhất có thể, tương thích cả Pydantic v1/v2
    và cả khi submodel (ChatbotContext) đang drop extra fields.
    Kèm theo DEBUG để thấy nó lấy được từ đâu.
    """
    # 0) nếu ctx có field hợp lệ
    ot = getattr(ctx, "origin_text", None)
    if isinstance(ot, str) and ot.strip():
        log.info("ask.debug.origin_text=hit.ctx_field val=%r", ot.strip())
        return ot.strip()

    # 1) pydantic v2 extra
    for extra_name in ("model_extra", "__pydantic_extra__"):
        extra = getattr(ctx, extra_name, None)
        if isinstance(extra, dict):
            v = extra.get("origin_text")
            if isinstance(v, str) and v.strip():
                log.info("ask.debug.origin_text=hit.ctx_%s val=%r", extra_name, v.strip())
                return v.strip()

    # 2) thử trực tiếp từ req.context nếu là dict
    raw_ctx = getattr(req, "context", None)
    if isinstance(raw_ctx, dict):
        v = raw_ctx.get("origin_text")
        if isinstance(v, str) and v.strip():
            log.info("ask.debug.origin_text=hit.req.context_dict val=%r", v.strip())
            return v.strip()

    # 3) thử đào __dict__ của context (kể cả khi là BaseModel)
    try:
        dctx = getattr(req, "context", None)
        if dctx is not None:
            dct = getattr(dctx, "__dict__", None)
            if isinstance(dct, dict):
                v = dct.get("origin_text")
                if isinstance(v, str) and v.strip():
                    log.info("ask.debug.origin_text=hit.ctx.__dict__ val=%r", v.strip())
                    return v.strip()
    except Exception:
        pass

    # 4) dump toàn bộ req: pydantic v2
    try:
        d = req.model_dump(mode="python", by_alias=True, exclude_none=False)
        ctx_dump = d.get("context") or {}
        if isinstance(ctx_dump, dict):
            v = ctx_dump.get("origin_text")
            if isinstance(v, str) and v.strip():
                log.info("ask.debug.origin_text=hit.req.model_dump val=%r", v.strip())
                return v.strip()
    except Exception:
        pass

    # 5) dump toàn bộ req: pydantic v1
    try:
        d = req.dict(by_alias=True)
        ctx_dump = d.get("context") or {}
        if isinstance(ctx_dump, dict):
            v = ctx_dump.get("origin_text")
            if isinstance(v, str) and v.strip():
                log.info("ask.debug.origin_text=hit.req.dict val=%r", v.strip())
                return v.strip()
    except Exception:
        pass

    # 6) đào __dict__ của chính req (khi FastAPI chưa strip hết)
    try:
        d = getattr(req, "__dict__", None)
        if isinstance(d, dict):
            ctx_dump = d.get("context")
            if isinstance(ctx_dump, dict):
                v = ctx_dump.get("origin_text")
                if isinstance(v, str) and v.strip():
                    log.info("ask.debug.origin_text=hit.req.__dict__ val=%r", v.strip())
                    return v.strip()
            # nếu context vẫn là model, thử thêm 1 lần
            if hasattr(ctx_dump, "__dict__"):
                v = ctx_dump.__dict__.get("origin_text")
                if isinstance(v, str) and v.strip():
                    log.info("ask.debug.origin_text=hit.req.__dict__.context.__dict__ val=%r", v.strip())
                    return v.strip()
    except Exception:
        pass

    log.info("ask.debug.origin_text=MISS")
    return None


def _resolve_lang(msg: str, ctx) -> str:
    # 1) respect FE context
    v = getattr(ctx, "lang", None)
    if isinstance(v, str) and v.lower() in ("en", "vi"):
        return v.lower()
    # 2) pydantic v2 extras (phòng trường hợp ctx drop extra)
    extra = getattr(ctx, "model_extra", None) or getattr(ctx, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        vv = extra.get("lang")
        if isinstance(vv, str) and vv.lower() in ("en", "vi"):
            return vv.lower()
    # 3) fallback: detect from message
    return _lang(msg)


# ========== Ranked raw (giữ need_confirm/candidates) ==========
async def _ranked_raw(
    ctx: ChatbotContext,
    lang: str,
    limit: int = 20,
    origin_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gọi search_ranked nhưng GIỮ nguyên need_confirm + candidates
    để chatbot có thể xử lý case origin mơ hồ giống hệt ranked.py.
    """
    radius_m = int((ctx.radius_km or 3.0) * 1000)
    mode = ctx.mode or "motorcycling"
    
    log.info("rank.debug.call radius_km=%.2f mode=%s",
         float(ctx.radius_km or 0), (ctx.mode or ""))

    return await search_ranked(
        origin=ctx.origin,
        origin_text=origin_text,          # <-- truyền origin_text vào intake_origin
        lat=None,
        lng=None,
        tags=ctx.tags or [],
        radius_m=radius_m,
        mode=mode,
        seed_cap=30,
        limit=limit,
        lang=lang,
        country="vn",
        weights=None,
        prior=None,
    )

# ========== Build prompt & gọi Gemini cho DETAIL_PLACE ==========
def _norm_txt(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

_CAFE_WORDS = {
    "cafe", "cà phê", "ca phe", "coffee", "coffee shop", "coffeeshop",
    "espresso", "roastery", "trà", "tea", "teahouse", "bakery"
}
_HOTEL_WORDS = {
    "hotel", "resort", "homestay", "hostel", "inn", "lodge", "lodging",
    "khach san", "khách sạn"
}

def _category_from_tags(p: Dict[str, Any], ctx: "ChatbotContext") -> str:
    # 1) override thẳng nếu payload nêu rõ
    cat_hint = (p.get("category") or p.get("cat") or "").strip().lower()
    if cat_hint in ("cafe", "coffee", "restaurant", "hotel"):
        return "cafe" if cat_hint in ("cafe", "coffee") else cat_hint

    # 2) gom tín hiệu: tags, ctx.tags, types/serp_types, name
    tokens = []
    tokens += [str(t) for t in (p.get("tags") or [])]
    tokens += [str(t) for t in (ctx.tags or [])]
    tokens += [str(t) for t in (p.get("types") or p.get("serp_types") or [])]
    tokens += [p.get("name") or ""]

    has_cafe = False
    has_hotel = False
    for t in tokens:
        nt = _norm_txt(t)
        if any(w in nt for w in _CAFE_WORDS):  has_cafe = True
        if any(w in nt for w in _HOTEL_WORDS): has_hotel = True

    if has_hotel and not has_cafe: return "hotel"
    if has_cafe  and not has_hotel: return "cafe"

    # 3) ưu tiên ctx.tags khi không chắc
    low_ctx = {_norm_txt(x) for x in (ctx.tags or [])}
    if any(w in low_ctx for w in _CAFE_WORDS):  return "cafe"
    if any(w in low_ctx for w in _HOTEL_WORDS): return "hotel"

    return "restaurant"

def _g_str():
    return {"type": "STRING"}

def _g_arr(item_schema):
    return {"type": "ARRAY", "items": item_schema}

def _detail_schema(cat: str) -> Dict[str, Any]:
    base = {
        "type": "OBJECT",
        "properties": {
            "highlight": _g_str(),
            "summary":  _g_str(),
            "price_hint": _g_str(),
            "best_time": _g_str(),
            "crowd":     _g_str(),
            "tips":      _g_arr(_g_str()),
        },
        "required": ["highlight", "summary"],  # (nếu lát nữa 400 nêu 'required' thì bỏ luôn)
    }
    if cat == "restaurant":
        base["properties"].update({
            "signature_dishes": _g_arr(_g_str()),
            "diet_notes": _g_str(),
        })
    elif cat == "cafe":
        base["properties"].update({
            "signature_drinks": _g_arr(_g_str()),
            "work_friendly": _g_str(),
        })
    else:  # hotel
        base["properties"].update({
            "services": _g_arr(_g_str()),
            "who_for": _g_str(),
        })
    return base


def _build_summary_prompt(
    p: Dict[str, Any],
    ctx: "ChatbotContext",
    lang: str
) -> Tuple[List[ChatMessage], Dict[str, Any]]:
    """
    Trả về (messages, generation_config_extra) để gọi Gemini ở JSON-mode.
    DÙNG ChatMessage(role, content) thay vì dict có 'parts'.
    - Phân ngành theo restaurant/cafe/hotel để kéo đúng thông tin user cần quyết định.
    """
    cat = _category_from_tags(p, ctx)
    name = p.get("name") or "địa điểm này"
    addr = p.get("address") or ""

    # 1) System prompt ngắn gọn, định hướng output
    if lang == "vi":
        sys_text = (
            "Bạn là trợ lý du lịch NestFeast. Trả lời xúc tích, hữu ích, bắt đầu bằng một câu hook khác-biệt. "
            "Nếu thông tin có thể thay đổi (giờ mở cửa/giá/dịch vụ), hãy ghi 'có thể thay đổi' hoặc 'ước lượng'. "
            "Không lặp lại kiểu 'X là một nhà hàng/cafe/khách sạn'."
        )
    else:
        sys_text = (
            "You are the NestFeast travel assistant. Be concise and useful, start with a distinctive hook. "
            "If facts may change (hours/prices/amenities), say 'estimate' or 'may change'. "
            "Do NOT say 'X is a restaurant/cafe/hotel'."
        )

    # 2) Yêu cầu theo ngành
    if lang == "vi":
        base = (
            f"Tóm tắt chi tiết {cat} **{name}** tại {addr}. "
            "Ưu tiên: điểm nổi bật, giá ước lượng, khi nào nên đi, mức độ đông/đợi, mẹo nhanh. "
        )
        if cat == "restaurant":
            extra_hint = "Nhấn mạnh MÓN NÊN THỬ và lưu ý khẩu phần/ăn kiêng nếu có."
        elif cat == "cafe":
            extra_hint = "Nhấn mạnh ĐỒ UỐNG NÊN THỬ và mức độ WORK-FRIENDLY (wifi/ổ cắm/độ ồn)."
        else:  # hotel
            extra_hint = "Nhấn mạnh DỊCH VỤ CHÍNH (hồ bơi/gym/spa/breakfast) và PHÙ HỢP VỚI đối tượng nào."
        if lang == "vi":
            ask = base + extra_hint + " Chỉ trả về JSON đúng schema yêu cầu."
        else:
            ask = base + extra_hint + " Return JSON only, following the schema."
    else:
        base = (
            f"Summarize details for the {cat} **{name}** at {addr}. "
            "Prioritize: key highlights, price hint, best time to go, crowd level, quick tips. "
        )
        if cat == "restaurant":
            extra_hint = "Emphasize SIGNATURE DISHES and any dietary notes if relevant."
        elif cat == "cafe":
            extra_hint = "Emphasize SIGNATURE DRINKS and WORK-FRIENDLINESS (wifi/outlets/noise)."
        else:
            extra_hint = "Emphasize KEY SERVICES (pool/gym/spa/breakfast) and WHO IT'S BEST FOR."
        if lang == "vi":
            ask = base + extra_hint + " Chỉ trả về JSON đúng schema yêu cầu."
        else:
            ask = base + extra_hint + " Return JSON only, following the schema."


    # 3) Messages theo ChatMessage(role, content)
    messages: List[ChatMessage] = [
        ChatMessage("system", sys_text),
        ChatMessage("user", ask),
    ]

    # 4) Ép JSON-mode với schema theo ngành để kéo đúng thông tin cần quyết định
    gen_cfg = {
        "response_mime_type": "application/json",
        "response_schema": _detail_schema(cat),
    }
    return messages, gen_cfg


def _render_text_from_json(data: Dict[str, Any], lang: str) -> str:
    # Chuyển JSON thành câu trả lời gọn: 1 câu hook + vài bullet
    bullets = []

    def add(key: str, label_vi: str, label_en: str):
        v = data.get(key)
        if not v:
            return
        if isinstance(v, list):
            v = ", ".join([str(x) for x in v if x])
        label = label_vi if lang == "vi" else label_en
        bullets.append(f"- **{label}**: {v}")

    hook = data.get("highlight") or ""
    if data.get("summary"):
        bullets.append(data["summary"])

    add("signature_dishes", "Món nên thử", "Signature dishes")
    add("signature_drinks", "Đồ uống nên thử", "Signature drinks")
    add("services", "Dịch vụ nổi bật", "Key services")
    add("price_hint", "Giá", "Price")
    add("best_time", "Nên đi lúc", "Best time")
    add("crowd", "Đông/Chờ đợi", "Crowd/Wait")
    add("work_friendly", "Làm việc/ổ cắm/wifi", "Work-friendly")
    add("diet_notes", "Lưu ý khẩu phần/ăn kiêng", "Diet notes")
    add("who_for", "Hợp với", "Best for")
    tips = data.get("tips") or []
    if tips:
        bullets.append(("**Mẹo:** " if lang == "vi" else "**Tips:** ") + "; ".join(tips[:3]))

    text = ""
    if hook:
        text += f"{hook}\n"
    if bullets:
        text += "\n".join(bullets)
    return text.strip()


async def _summarize_with_gemini(
    p: Dict[str, Any],
    ctx: "ChatbotContext",
    lang: str,
) -> tuple[str, Optional[dict], bool]:
    """
    Tóm tắt chi tiết 1 địa điểm bằng Gemini ở JSON-mode để ép model cung cấp
    thông tin quan trọng (món/dịch vụ, giá, best time, crowd, tips).
    Trả về (summary_text, usage_dict|None, is_llm).
    """
    # ——— DEBUG: in/out + context ———
    try:
        cat_dbg = _category_from_tags(p, ctx)
    except Exception:
        cat_dbg = "?"
    log.info(
        "gemini.detail: name=%s cat=%s tags=%s model=%s",
        p.get("name"), cat_dbg, ctx.tags, _DEF_GEMINI_MODEL
    )

    try:
        msgs, extra = _build_summary_prompt(p, ctx, lang)

        async def _call(max_tok: int, use_schema: bool):
            return await gemini_chat(
                msgs,
                temperature=0.3,
                max_output_tokens=max_tok,
                response_mime_type=("application/json" if use_schema else None),
                response_schema=(extra["response_schema"] if use_schema else None),
                model=_DEF_GEMINI_MODEL,
            )

        # Lần 1: JSON-mode + schema (chuẩn)
        res = await _call(2000, True)

        # Log kết quả lần 1
        raw = (getattr(res, "text", None) or "").strip()
        raw_preview = (raw[:300] + "…") if len(raw) > 300 else raw
        log.info(
            "gemini.result: error=%s finish=%s usage=%s text_len=%d preview=%s",
            getattr(res, "error", None),
            getattr(res, "finish_reason", None),
            getattr(res, "usage", None),
            len(raw),
            raw_preview.replace("\n", " ") if raw_preview else ""
        )
        if getattr(res, "raw", None) and isinstance(res.raw, dict) and res.raw.get("error"):
            log.warning("gemini.provider_error=%s", res.raw.get("provider_error"))

        def _try_parse(s: str):
            try:
                return json.loads(_strip_code_fence(s))
            except Exception as e:
                log.info("gemini.json_parse_failed: %s", str(e))
                return None

        data = _try_parse(raw) if raw else None

        if isinstance(data, dict) and (data.get("summary") or data.get("highlight")):
            text = _render_text_from_json(data, lang)
            if text:
                log.info("detail_place.used=gemini-json place=%s", p.get("name"))
                return text, (getattr(res, "usage", None) or None), True

        # Lần 2 (chỉ khi lần 1 rỗng hoặc đụng MAX_TOKENS): nới tokens + bỏ schema để trả text tự do
        if not raw or getattr(res, "finish_reason", "") == "MAX_TOKENS":
            log.info("gemini.retry: widen_tokens_or_drop_schema place=%s", p.get("name"))
            res2 = await _call(1024, False)
            raw2 = (getattr(res2, "text", None) or "").strip()
            if raw2:
                log.info("detail_place.used=gemini-text place=%s", p.get("name"))
                return raw2, (getattr(res2, "usage", None) or None), True

    except Exception:
        log.exception("gemini summary failed")

    # ---- Fallback rule-based (khi LLM lỗi/không có text) ----
    name = p.get("name") or "—"
    dist_km = None
    try:
        if p.get("distance_m") is not None:
            dist_km = round(float(p["distance_m"]) / 1000.0, 2)
    except Exception:
        pass
    eta_min = None
    try:
        if p.get("eta_s") is not None:
            eta_min = float(p["eta_s"]) / 60.0
    except Exception:
        pass

    rating_str = _rating(p.get("rating"), p.get("user_ratings_total"))
    fallback_text = (
        f"**{name}** • Thời gian: {_mins(eta_min)}; Quãng đường: {_km(dist_km)} • Đánh giá: {rating_str}"
        if lang == "vi"
        else f"**{name}** • ETA: {_mins(eta_min)}; Distance: {_km(dist_km)} • Rating: {rating_str}"
    )
    log.info("detail_place.used=rule place=%s (fallback: empty_or_error)", p.get("name"))
    return fallback_text, None, False


# ========== Entry point chính ==========

# ========== Ranked wrapper (chỉ lấy items) ==========
async def _run_ranked(
    ctx: ChatbotContext,
    lang: str,
    limit: int = 20,
    origin_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Wrapper tiện dụng khi ta chỉ cần danh sách quán (items) để hiển thị,
    không cần need_confirm/candidates.
    """
    ranked = await _ranked_raw(ctx, lang, limit=limit, origin_text=origin_text)
    items = ranked.get("items") or []
    return [_coerce_place_dict(x) for x in items]


# ========== Entry point chính ==========

async def handle_chat(req: ChatbotRequest) -> ChatbotResponse:
    ctx = req.context or ChatbotContext()
    lang = _resolve_lang(req.message, ctx)

    # log context FE gửi vào (tuyệt đối quan trọng để bắt lỗi này)
    log.info("ask.debug.ctx_in=%s", _ctx_dbg(ctx))
    
    # 1) Detect intent từ message + context
    intent, payload = _detect_intent(req.message, ctx)

    log.info(
        "ask.debug.entry msg=%r intent=%s ctx.origin=%s radius=%.2f mode=%s tags=%s",
        (req.message or ""),
        getattr(intent, "name", str(intent)),
        getattr(ctx, "origin", None),
        float(ctx.radius_km or 0),
        (ctx.mode or ""),
        (ctx.tags or []),
    )

    # 2) Cập nhật context lightweight (mode / radius)
    if intent == ChatbotIntent.CHANGE_RADIUS and "radius_km" in payload:
        # yêu cầu người dùng
        try:
            req_r = float(payload["radius_km"])
        except Exception:
            req_r = float(ctx.radius_km or 3.0)

        # kẹp về [1, 7]
        r = max(1.0, min(7.0, req_r))
        ctx.radius_km = r

        # ghi chú để giải thích ở bước reply
        note_vi = note_en = ""
        if r != req_r:
            if req_r > 7:
                note_vi = " (vượt quá 7 km nên mặc định đặt 7 km)"
                note_en = " (above 7 km, so clamped to 7 km)"
            elif req_r <= 0:
                note_vi = " (≤ 0 km nên mặc định đặt 1 km)"
                note_en = " (≤ 0 km, so clamped to 1 km)"
        setattr(ctx, "_radius_note_vi", note_vi)
        setattr(ctx, "_radius_note_en", note_en)
    if intent == ChatbotIntent.CHANGE_MODE and "mode" in payload:
        ctx.mode = str(payload["mode"])

    # 3) Lấy origin_text an toàn từ request/context
    origin_text = _get_origin_text(req, ctx)
    log.info(
        "ask.debug.intent=%s origin_text=%r",
        getattr(intent, "name", str(intent)),
        origin_text,
    )
    # Lưu vào ctx để các lượt sau _detect_intent thấy được
    if origin_text:
        try:
            setattr(ctx, "origin_text", origin_text)
        except Exception:
            pass

    # 4) Nếu intent là CLARIFY_ORIGIN mà thật sự không có cả origin & origin_text
    #    → chỉ nhắc user nhập địa chỉ.
    if (
        intent == ChatbotIntent.CLARIFY_ORIGIN
        and ctx.origin is None
        and not origin_text
    ):
        log.info("ask.debug.early_return=clarify_origin_no_origin_text")
        return ChatbotResponse(
            reply=(
                "Bạn vui lòng cung cấp địa chỉ xuất phát (vd: 'Trường Đại học Khoa học Tự nhiên - ĐHQG TP.HCM - Cơ sở 1')."
                if lang == "vi"
                else "Please tell me your starting location."
            ),
            intent=intent,
            chips=[],
            context=ctx,
        )
        
    # 4b) Chỉ ĐỔI BÁN KÍNH → xác nhận, KHÔNG tự auto gợi ý lại
    if intent == ChatbotIntent.CHANGE_RADIUS:
        r = float(ctx.radius_km or 0)
        note_vi = getattr(ctx, "_radius_note_vi", "")
        note_en = getattr(ctx, "_radius_note_en", "")
        if lang == "vi":
            reply = f"Bán kính hiện tại đang là {r:g} km{note_vi}."
        else:
            reply = f"Current radius is {r:g} km{note_en}."
        log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
        return ChatbotResponse(
            reply=reply,
            intent=intent,
            chips=[],
            context=ctx,
        )

    # 4c) Chỉ ĐỔI MODE → xác nhận, KHÔNG tự auto gợi ý lại
    if intent == ChatbotIntent.CHANGE_MODE:
        mode_label_vi = {
            "walking": "đi bộ",
            "motorcycling": "xe máy",
            "driving": "ô tô",
            "truck": "xe tải", 
        }.get(ctx.mode or "", ctx.mode or "phương tiện này")
        if lang == "vi":
            reply = f"Đã chuyển sang chế độ di chuyển bằng {mode_label_vi}."
        else:
            reply = f"Travel mode set to {ctx.mode or 'this mode'}."
        log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
        return ChatbotResponse(
            reply=reply,
            intent=intent,
            chips=[],
            context=ctx,
        )

    # 5) Những intent sẽ kéo recommend / gọi ranked
    should_reco = intent in (
        ChatbotIntent.QUICK_RECOMMEND,
        ChatbotIntent.NONE,
        ChatbotIntent.CLARIFY_ORIGIN,   # <- cho phép CLARIFY_ORIGIN đi vào ranked nếu có origin_text
    )

    if should_reco:
        # Vẫn chặn nếu không có cả origin lẫn origin_text
        if ctx.origin is None and not origin_text:
            log.info("ask.debug.blocked=no_origin_and_no_origin_text -> clarify")
            reply = (
                "Bạn vui lòng nhập vị trí xuất phát trước nhé."
                if lang == "vi"
                else "Please set your starting location first."
            )
            return ChatbotResponse(
                reply=reply,
                intent=ChatbotIntent.CLARIFY_ORIGIN,
                chips=[],
                context=ctx,
            )
            
        # NEW: Nếu CHƯA có origin nhưng CÓ origin_text → thử Autocomplete trước
        if ctx.origin is None and origin_text:
            ac_resp = await _origin_candidates_via_autocomplete(origin_text, ctx, lang)
            if ac_resp is not None:
                log.info(
                    "ask.debug.return_candidates=via_autocomplete n=%d",
                    len(ac_resp.chips or [])
                )
                return ac_resp

        # Có origin_text hoặc origin rồi → gọi ranked
        log.info(
            "ask.debug.call_ranked origin=%s origin_text=%r",
            ctx.origin,
            origin_text,
        )
        ranked_raw = await _ranked_raw(ctx, lang, limit=20, origin_text=origin_text)

        log.info(
            "ask.debug.rank_result need_confirm=%s n_candidates=%d has_origin=%s n_items=%d",
            bool(ranked_raw.get("need_confirm")),
            len(ranked_raw.get("candidates") or []),
            bool(ranked_raw.get("origin")),
            len(ranked_raw.get("items") or []),
        )

        # 5a) ORIGIN MƠ HỒ → need_confirm + candidates
        # NEW: Ưu tiên Autocomplete nếu intake_origin vẫn 'need_confirm'
        if ranked_raw.get("need_confirm") and origin_text:
            ac_resp = await _origin_candidates_via_autocomplete(origin_text, ctx, lang)
            if ac_resp is not None:
                log.info(
                    "ask.debug.return_candidates=ranked_need_confirm_but_use_autocomplete n=%d",
                    len(ac_resp.chips or [])
                )
                return ac_resp
    
        if ranked_raw.get("need_confirm"):
            cands = ranked_raw.get("candidates") or []

            chips: List[ChatChip] = []
            # candidates từ intake_origin đã được scoring + sort giảm dần score,
            # ở đây chỉ cần cắt top 3
            for c in cands[:3]:
                default_label = "Chọn địa chỉ này" if lang == "vi" else "Choose this address"
                label = (
                    c.get("display_address") or c.get("full_address") or c.get("name") or default_label
                )
                
                chips.append(
                    ChatChip(
                        label=label,
                        intent=ChatbotIntent.QUICK_RECOMMEND,   # FE set ctx.origin rồi hỏi lại
                        payload={
                            "origin": {
                                "lat": c.get("lat"),
                                "lng": c.get("lng"),
                                "kind": "point",
                                "place_id": c.get("place_id"),
                                "display_address": c.get("display_address") or c.get("name"),
                                "source": "geocode",
                                "confidence": c.get("confidence", 0.0),
                            }
                        },
                    )
                )

            note = (
                "Mình tìm được vài vị trí, bạn chọn đúng địa chỉ xuất phát nhé."
                if lang == "vi"
                else "I found a few possible starting points—please pick the correct one."
            )
            log.info("ask.debug.return_candidates n=%d", len(chips))
            return ChatbotResponse(
                reply=note,
                intent=ChatbotIntent.CLARIFY_ORIGIN,
                chips=chips,
                context=ctx,
            )

        # 5b) Đã có origin rõ ràng → lưu lại vào context
        if ranked_raw.get("origin"):
            ctx.origin = ranked_raw["origin"]

        # 5c) Lấy danh sách quán và format top 3
        items = ranked_raw.get("items") or []
        places = [_coerce_place_dict(x) for x in items]
        ctx.active_place_ids = [
            p.get("place_id") for p in places[:10] if p.get("place_id")
        ]

        reply = _format_top3_reply(places, lang)
        chips: List[ChatChip] = []
        
        # Chi tiết #1–#3
        for i in range(1, min(3, len(places)) + 1):
            chips.append(
                ChatChip(
                    label=(f"Chi tiết #{i}" if lang == "vi" else f"Details #{i}"),
                    intent=ChatbotIntent.DETAIL_PLACE,
                    payload={"index": i},
                )
            )

        chips.extend(_chips_basic(ctx, lang))
        log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
        return ChatbotResponse(
            reply=reply,
            intent=ChatbotIntent.QUICK_RECOMMEND,
            chips=chips,
            context=ctx,
        )

    # ================== PICK_PLACE ==================
    if intent == ChatbotIntent.PICK_PLACE:
        idx_raw = payload.get("index", 1)
        try:
            idx = int(idx_raw)
        except Exception:
            idx = 1

        if not ctx.active_place_ids:
            reply = (
                "Bạn hãy yêu cầu gợi ý gần đây trước đã (vd: 'gợi ý cafe gần đây')."
                if lang == "vi"
                else "Please ask for nearby suggestions first (e.g., 'suggest nearby cafes')."
            )
            log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
            return ChatbotResponse(
                reply=reply,
                intent=ChatbotIntent.NONE,
                chips=_chips_basic(ctx, lang),
                context=ctx,
            )

        places = await _run_ranked(ctx, lang, origin_text=origin_text)
        if not places:
            reply = (
                "Không có dữ liệu để hiển thị."
                if lang == "vi"
                else "No data to display."
            )
            log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
            return ChatbotResponse(
                reply=reply,
                intent=ChatbotIntent.NONE,
                chips=_chips_basic(ctx, lang),
                context=ctx,
            )

        if idx < 1 or idx > len(places):
            idx = 1
        p = places[idx - 1]

        name = p.get("name") or "—"
        dist_km = (
            round(float(p["distance_m"]) / 1000.0, 2)
            if p.get("distance_m") is not None
            else None
        )
        eta_min = (
            float(p["eta_s"]) / 60.0 if p.get("eta_s") is not None else None
        )
        rating = _rating(p.get("rating"), p.get("user_ratings_total"))

        if lang == "vi":
            reply = f"**{name}** • Thời gian: {_mins(eta_min)}; Quãng đường: {_km(dist_km)} • Đánh giá: {rating}"
        else:
            reply = f"**{name}** • ETA: {_mins(eta_min)}; Distance: {_km(dist_km)} • Rating: {rating}"

        chips: List[ChatChip] = []
        for i in range(1, min(3, len(places)) + 1):
            chips.append(
                ChatChip(
                    label=(f"Chi tiết #{i}" if lang == "vi" else f"Details #{i}"),
                    intent=ChatbotIntent.DETAIL_PLACE,
                    payload={"index": i},
                )
            )
        chips.extend(_chips_basic(ctx, lang))
        log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
        return ChatbotResponse(
            reply=reply,
            intent=ChatbotIntent.PICK_PLACE,
            chips=chips,
            context=ctx,
        )


    # Chi tiết 1 địa điểm → gọi Gemini để tóm tắt
    if intent == ChatbotIntent.DETAIL_PLACE:
    # (A) Cho phép "đi tắt theo tên" khi chưa có danh sách
        qname = (payload.get("name") or "").strip() if isinstance(payload, dict) else ""
        if not ctx.active_place_ids and qname:
            # tạo place tối thiểu để tóm tắt
            p = {"name": qname, "address": payload.get("address") or ""}
            summary, usage, is_llm = await _summarize_with_gemini(p, ctx, lang)
            log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
            return ChatbotResponse(
                reply=summary,
                intent=ChatbotIntent.DETAIL_PLACE,
                chips=_chips_basic(ctx, lang),
                context=ctx,
                detail=PlaceDetailSummary(
                    name=qname,
                    summary=summary,
                    source=("gemini" if is_llm else "rule"),
                    llm_usage=usage,
                ),
            )

        # (B) Có danh sách → lấy theo index/tên mờ
        places = await _run_ranked(ctx, lang, origin_text=origin_text)
        if not places:
            reply = "Không có dữ liệu để hiển thị." if lang == "vi" else "No data to display."
            log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
            return ChatbotResponse(reply=reply, intent=ChatbotIntent.NONE, chips=_chips_basic(ctx, lang), context=ctx)

        p: Optional[Dict[str, Any]] = None
        idx_raw = payload.get("index")
        if idx_raw is not None:
            try:
                i = int(idx_raw)
                if 1 <= i <= len(places):
                    p = places[i - 1]
            except Exception:
                pass

        if p is None and qname:
            low = qname.lower()
            for cand in places:
                if (cand.get("name") or "").lower().find(low) >= 0:
                    p = cand
                    break

        if p is None:
            p = places[0]

        summary, usage, is_llm = await _summarize_with_gemini(p, ctx, lang)
        chips: List[ChatChip] = []
        chips.extend(_chips_basic(ctx, lang))
        log.info("ask.debug.ctx_out=%s", _ctx_dbg(ctx))
        return ChatbotResponse(
            reply=summary,
            intent=ChatbotIntent.DETAIL_PLACE,
            chips=chips,
            context=ctx,
            detail=PlaceDetailSummary(
                place_id=p.get("place_id"),
                name=p.get("name"),
                address=p.get("address"),
                tags=p.get("tags") or [],
                rating=p.get("rating"),
                user_ratings_total=p.get("user_ratings_total"),
                eta_s=p.get("eta_s"),
                distance_m=p.get("distance_m"),
                summary=summary,
                source=("gemini" if is_llm else "rule"),
                llm_usage=usage,
            ),
        )
