from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .client import SerpApiClient


# ---- Model seed tối giản cho ranking/UI ----
@dataclass
class PlaceCandidate:
    place_id: str
    name: str
    lat: float
    lng: float
    address: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None


def _to_int(x: Any) -> Optional[int]:
    try:
        if isinstance(x, str):
            return int(x.replace(",", "").strip())
        return int(x)
    except Exception:
        return None


def _parse_local_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    # SerpAPI trả local_results[] hoặc organic_results[]
    if isinstance(data.get("local_results"), list):
        return data["local_results"]
    if isinstance(data.get("organic_results"), list):
        return data["organic_results"]
    return []


async def nearby_search(
    origin_lat: float,
    origin_lng: float,
    *,
    radius_km: float = 2.0,                     # giữ tham số để services có thể cắt bán kính bằng Haversine
    tags: Sequence[str] = ("restaurant",),     # restaurant | cafe | hotel
    language: str = "vi",
    country: str = "vn",
    page_size: int = 20,                        # theo spec: trả tối đa 20
    client: Optional[SerpApiClient] = None,
) -> List[PlaceCandidate]:
    """
    Provider SerpAPI (engine=google_maps, type=search):
    - Trả danh sách seed POI quanh (lat,lng) cho 1 tag.
    - Chỉ điền các trường: name, address, lat/lng, rating, user_ratings_total.
    - Cắt bán kính & ranking sẽ làm ở tầng services.
    """
    owns = client is None
    client = client or SerpApiClient()

    try:
        tag = tags[0] if tags else "restaurant"
        params = {
            "engine": "google_maps",
            "type": "search",
            "q": tag,
            "hl": language,
            "gl": country,
            "ll": f"@{origin_lat},{origin_lng},14z",
        }
        data = await client.get_json(params)
        rows = _parse_local_results(data)

        out: List[PlaceCandidate] = []
        for it in rows[:max(1, page_size)]:
            pid = it.get("data_id") or it.get("place_id") or it.get("cid") or ""
            gps = it.get("gps_coordinates") or {}
            lat = gps.get("latitude")
            lng = gps.get("longitude")
            try:
                lat = float(lat)
                lng = float(lng)
            except Exception:
                continue

            # rating / total_ratings (nhiều kết quả chỉ có 'reviews')
            r = it.get("rating")
            try:
                r = float(r) if r is not None else None
            except Exception:
                r = None

            n_reviews = _to_int(it.get("user_ratings_total") or it.get("reviews"))

            out.append(
                PlaceCandidate(
                    place_id=pid,
                    name=it.get("title") or it.get("name") or "",
                    lat=lat,
                    lng=lng,
                    address=it.get("address") or it.get("full_address") or it.get("subtitle"),
                    rating=r,
                    user_ratings_total=n_reviews,
                )
            )

        return out[:page_size]
    finally:
        if owns:
            await client.aclose()


# ---- Quick manual test (chạy trực tiếp file) ----
if __name__ == "__main__":
    import asyncio, os
    async def _demo():
        print("HAS_KEY?", bool(os.getenv("SERPAPI_KEY") or os.getenv("SERP_API_KEY")))
        seeds = await nearby_search(
            origin_lat=10.875620, origin_lng=106.799140,
            tags=("restaurant",), page_size=8
        )
        print("FOUND:", len(seeds))
        for s in seeds:
            print(f"{s.name} | rating={s.rating} | reviews={s.user_ratings_total}")
    asyncio.run(_demo())
