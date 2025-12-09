from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .client import TrackAsiaClient

# Endpoint Autocomplete v2 (Google-format)
# Docs: https://docs.track-asia.com/vi/api-integration/autocomplete/v2/
_PATH_V2 = "api/v2/place/autocomplete/json"


# ---------- Models ----------
@dataclass
class AutocompleteCandidate:
    """
    Gợi ý autocomplete dùng để render dropdown trong UI.

    - main_text: dòng chính (tên địa điểm / số nhà + tên đường)
    - secondary_text: dòng phụ (quận/huyện, tỉnh/thành, country…)
    - description: full text (có thể dùng cho debug / tooltip)
    - lat/lng: có thể có hoặc không (tùy backend; VN có thể không trả)
    - types: list type thô từ API (['establishment', 'route', ...])
    - official_id: mã hành chính (nếu là đơn vị hành chính)
    - old_*: địa chỉ cũ (nếu bạn bật new_admin + include_old_admin)
    """
    place_id: str
    main_text: str
    secondary_text: str
    description: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    types: Optional[List[str]] = None
    official_id: Optional[str] = None
    old_description: Optional[str] = None
    old_formatted_address: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


# ---------- Helpers ----------
def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _ensure_str(x: Any, max_len: int) -> str:
    if x is None:
        return ""
    s = str(x)
    if max_len and len(s) > max_len:
        s = s[:max_len]
    return s


# ---------- Main function ----------
async def autocomplete_v2(
    query: str,
    *,
    location: Optional[Tuple[float, float]] = None,  # (lat, lng) để bias kết quả quanh 1 điểm
    size: int = 5,                                   # số gợi ý tối đa
    new_admin: bool = True,                          # ưu tiên địa giới hành chính mới
    include_old_admin: bool = False,                 # có trả thêm địa chỉ cũ không
    client: Optional[TrackAsiaClient] = None,
) -> List[AutocompleteCandidate]:
    """
    Gọi TrackAsia Autocomplete v2 một cách an toàn, không raise lỗi HTTP.

    - Trả về [] nếu có lỗi mạng / HTTP / JSON.
    - Chỉ dùng GET {BASE}/api/v2/place/autocomplete/json.
    - Tự thêm ?key=... qua TrackAsiaClient.

    Parameters
    ----------
    query:
        Chuỗi người dùng đang gõ trong ô địa chỉ.
    location:
        (lat, lng) để bias gợi ý quanh một khu vực (ví dụ origin hiện tại).
    size:
        Số lượng gợi ý tối đa trả về.
    new_admin / include_old_admin:
        Điều khiển địa giới hành chính theo docs TrackAsia.
    """
    owns = client is None
    client = client or TrackAsiaClient()

    params: Dict[str, Any] = {
        "input": query,
        "size": max(1, size),
    }

    # Bias theo vị trí hiện tại (lat,lng)
    if location is not None:
        lat, lng = location
        params["location"] = f"{float(lat)},{float(lng)}"

    # Địa giới hành chính mới/cũ
    if new_admin:
        params["new_admin"] = "true"
        if include_old_admin:
            params["include_old_admin"] = "true"

    try:
        data = await client.get_json(_PATH_V2, params=params)
        if not isinstance(data, dict) or data.get("error"):
            # có thể log data["error"] ở tầng services
            return []

        status = _ensure_str(data.get("status"), 16).upper()
        if status and status != "OK":
            # ZERO_RESULTS hoặc bất kỳ status khác → coi như không có gợi ý
            return []

        predictions = data.get("predictions") or []
        out: List[AutocompleteCandidate] = []

        for it in predictions:
            sf = it.get("structured_formatting") or {}
            main_text = (
                sf.get("main_text")
                or it.get("name")
                or it.get("description")
                or ""
            )
            secondary_text = (
                sf.get("secondary_text")
                or it.get("sublabel")
                or ""
            )

            description = (
                it.get("description")
                or it.get("formatted_address")
                or main_text
            )

            # sau đoạn đọc geom:
            geom = (it.get("geometry") or {}).get("location") or {}
            lat = _to_float(geom.get("lat"))
            lng = _to_float(geom.get("lng"))

            # bỏ qua mọi suggestion không có toạ độ
            #if lat is None or lng is None or (lat == 0.0 and lng == 0.0):
            #   continue

            types_val = it.get("types")
            types: Optional[List[str]] = None
            if isinstance(types_val, list):
                types = [str(x) for x in types_val][:10]

            cand = AutocompleteCandidate(
                place_id=_ensure_str(
                    it.get("place_id") or it.get("official_id") or it.get("reference") or "",
                    128,
                ),
                main_text=_ensure_str(main_text, 256),
                secondary_text=_ensure_str(secondary_text, 256),
                description=_ensure_str(description, 512),
                lat=lat,
                lng=lng,
                types=types,
                official_id=_ensure_str(it.get("official_id") or "", 64) or None,
                old_description=_ensure_str(it.get("old_description") or "", 512) or None,
                old_formatted_address=_ensure_str(
                    it.get("old_formatted_address") or "", 512
                )
                or None,
                raw=it,
            )
            out.append(cand)

        return out[: max(0, size)]

    finally:
        if owns:
            await client.aclose()


# ---------- Quick manual test ----------
if __name__ == "__main__":
    async def _demo():
        import os

        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",  # nhớ thay bằng key thật khi deploy
            http2=bool(int(os.getenv("TRACKASIA_HTTP2", "0"))),
            max_retries=1,
        ) as c:
            res = await autocomplete_v2(
                "Landmark 81",
                location=(10.7952219, 106.7217912),
                size=5,
                new_admin=True,
                include_old_admin=True,
                client=c,
            )
            print("Got", len(res), "prediction(s).")
            for i, v in enumerate(res, 1):
                print(
                    f"{i}. {v.main_text} | {v.secondary_text} | "
                    f"{v.lat},{v.lng} | place_id={v.place_id}"
                )

    asyncio.run(_demo())
