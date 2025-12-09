from __future__ import annotations

"""
Service cho tính năng Autocomplete địa chỉ / địa điểm.

Nhiệm vụ:
- Nhận query user đang gõ + optional tọa độ bias.
- Gọi providers.trackasia.autocomplete.autocomplete_v2.
- Chuẩn hóa về model AutocompleteSuggestion đơn giản cho API / FE.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import asyncio
from app.providers.trackasia.autocomplete import (
    AutocompleteCandidate,
    autocomplete_v2,
)

from app.providers.trackasia.geocode import forward_geocode

# ===== Domain model cho tầng service (không phụ thuộc FastAPI / Pydantic) =====
@dataclass
class AutocompleteSuggestion:
    """Gợi ý autocomplete dùng để render dropdown 2 dòng trong FE."""

    place_id: str

    # Dòng chính (in đậm): tên địa điểm / số nhà + tên đường
    main_text: str

    # Dòng phụ (xám): quận/huyện, tỉnh/thành, country...
    secondary_text: str

    # Mô tả đầy đủ (có thể dùng cho tooltip / debug)
    description: str

    lat: Optional[float] = None
    lng: Optional[float] = None

    # Các field extra nếu cần mở rộng sau
    official_id: Optional[str] = None
    old_description: Optional[str] = None
    old_formatted_address: Optional[str] = None

    # raw để debug (KHÔNG trả ra API, chỉ dùng nội bộ nếu cần)
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển sang dict để API / Pydantic dễ dùng."""
        d = asdict(self)
        # raw chỉ để debug, thường không trả về client
        d.pop("raw", None)
        return d


# ===== Helper chuyển từ provider model sang service model =====
def _map_candidate(c: AutocompleteCandidate) -> AutocompleteSuggestion:
    return AutocompleteSuggestion(
        place_id=c.place_id,
        main_text=c.main_text or c.description or "",
        secondary_text=c.secondary_text or "",
        description=c.description or "",
        lat=c.lat,
        lng=c.lng,
        official_id=c.official_id,
        old_description=c.old_description,
        old_formatted_address=c.old_formatted_address,
        raw=c.raw,
    )

def _has_coords(c: AutocompleteCandidate) -> bool:
    """True nếu candidate có lat/lng hợp lệ (không None, không 0,0)."""
    try:
        return (
            c.lat is not None and c.lng is not None and
            not (abs(float(c.lat)) < 1e-9 and abs(float(c.lng)) < 1e-9)
        )
    except Exception:
        return False
    
async def _fill_missing_coords(cands: List[AutocompleteCandidate],
                               per_query_limit: int = 3) -> List[AutocompleteCandidate]:
    """
    Với những candidate autocomplete thiếu toạ độ:
      - Gọi forward_geocode(description/main_text, limit=1) để lấy lat/lng.
      - Giới hạn số cuộc gọi geocode / query để tránh bắn quá nhiều API.
      - Candidate nào vẫn không có toạ độ thì bị loại.
    """
    # 1) Tách ra những thằng thiếu toạ độ
    todo: List[Tuple[int, AutocompleteCandidate]] = [
        (i, c) for i, c in enumerate(cands) if not _has_coords(c)
    ]
    if not todo:
        return [c for c in cands if _has_coords(c)]

    # Chỉ xử lý tối đa per_query_limit candidate / query cho an toàn
    todo = todo[: per_query_limit]

    async def _fix_one(idx: int, c: AutocompleteCandidate):
        text = (c.description or c.main_text or "").strip()
        if len(text) < 3:
            return
        try:
            res = await forward_geocode(text, limit=1)
        except Exception:
            return
        if not res:
            return
        best = res[0]
        try:
            lat = float(getattr(best, "lat"))
            lng = float(getattr(best, "lng"))
        except Exception:
            return
        if not (abs(lat) < 1e-9 and abs(lng) < 1e-9):
            c.lat = lat
            c.lng = lng

    # Giới hạn concurrency 3 để đỡ “nổ” provider
    sem = asyncio.Semaphore(3)

    async def _wrapped(idx: int, c: AutocompleteCandidate):
        async with sem:
            await _fix_one(idx, c)

    await asyncio.gather(*[_wrapped(i, c) for i, c in todo])

    # Bỏ mọi candidate vẫn không có toạ độ
    return [c for c in cands if _has_coords(c)]

# ===== Public API cho các router / service khác dùng =====
async def suggest_places(
    query: str,
    *,
    center: Optional[Tuple[float, float]] = None,
    limit: int = 5,
    new_admin: bool = True,
    include_old_admin: bool = False,
) -> List[AutocompleteSuggestion]:
    """
    Gợi ý autocomplete cho input `query`.

    Parameters
    ----------
    query:
        Chuỗi user đang gõ trong ô input.
    center:
        (lat, lng) để bias kết quả quanh một điểm (ví dụ: origin hiện tại).
    limit:
        Số gợi ý tối đa.
    new_admin / include_old_admin:
        Điều khiển địa giới hành chính theo docs TrackAsia.

    Returns
    -------
    List[AutocompleteSuggestion]:
        Danh sách gợi ý đã chuẩn hóa; [] nếu có lỗi hoặc không có kết quả.
    """
    if not query or not query.strip():
        return []

    try:
        # 1) Gọi Autocomplete v2 như cũ
        candidates = await autocomplete_v2(
            query=query.strip(),
            location=center,
            size=limit,
            new_admin=new_admin,
            include_old_admin=include_old_admin,
        )
    except Exception:
        # provider layer hầu như đã nuốt lỗi, nhưng để chắc chắn:
        return []
    
    # 2) Bù toạ độ cho những candidate thiếu lat/lng
    try:
        candidates = await _fill_missing_coords(candidates, per_query_limit=3)
    except Exception:
        # Nếu việc bù toạ độ lỗi thì vẫn dùng danh sách cũ (lọc những thằng có sẵn toạ độ)
        candidates = [c for c in candidates if _has_coords(c)]

    # 3) Map sang AutocompleteSuggestion; nếu vẫn rỗng thì trả [] luôn
    return [_map_candidate(c) for c in candidates if _has_coords(c)]


async def suggest_places_as_dict(
    query: str,
    *,
    center: Optional[Tuple[float, float]] = None,
    limit: int = 5,
    new_admin: bool = True,
    include_old_admin: bool = False,
) -> List[Dict[str, Any]]:
    """
    Giống suggest_places nhưng trả List[dict] – tiện dùng trực tiếp trong API router.
    """
    suggestions = await suggest_places(
        query=query,
        center=center,
        limit=limit,
        new_admin=new_admin,
        include_old_admin=include_old_admin,
    )
    return [s.to_dict() for s in suggestions]


def suggest_places_sync(
    query: str,
    *,
    center: Optional[Tuple[float, float]] = None,
    limit: int = 5,
    new_admin: bool = True,
    include_old_admin: bool = False,
) -> List[Dict[str, Any]]:
    """
    Wrapper sync – tiện gọi từ Streamlit.
    Trả List[dict] giống suggest_places_as_dict.
    - Nếu không có event loop: dùng asyncio.run
    - Nếu đang có event loop (ít gặp): chạy coroutine trong 1 thread riêng.
    """
    async def _coro():
        return await suggest_places_as_dict(
            query=query,
            center=center,
            limit=limit,
            new_admin=new_admin,
            include_old_admin=include_old_admin,
        )

    try:
        # Trường hợp bình thường (không có loop đang chạy) — phù hợp với Streamlit
        return asyncio.run(_coro())
    except RuntimeError:
        # Có loop đang chạy -> dùng thread phụ để tạo loop mới an toàn
        from threading import Thread
        from queue import Queue

        q: "Queue[List[Dict[str, Any]]]" = Queue()

        def runner():
            q.put(asyncio.run(_coro()))

        t = Thread(target=runner, daemon=True)
        t.start()
        t.join()
        return q.get()
        
        
# ====== Demo thủ công (chạy trực tiếp file này) ======
if __name__ == "__main__":
    import asyncio

    async def _demo():
        res = await suggest_places("Landmark 81", center=(10.7952, 106.7218))
        print("Got", len(res), "suggestion(s).")
        for i, s in enumerate(res, 1):
            print(
                f"{i}. {s.main_text} | {s.secondary_text} | "
                f"{s.lat},{s.lng} | place_id={s.place_id}"
            )

    asyncio.run(_demo())
