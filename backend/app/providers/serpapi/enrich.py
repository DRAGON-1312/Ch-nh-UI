# SerpAPI provider (engine=google_maps) 
# rating & user_ratings_total
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import SerpApiClient
from .nearby import PlaceCandidate


# ===== Models: chỉ 2 tín hiệu cho ranking =====
@dataclass
class RankSignals:
    place_id: str
    rating: Optional[float]
    user_ratings_total: Optional[int]


# ===== Helpers =====
def _to_int(x: Any) -> Optional[int]:
    try:
        if isinstance(x, str):
            return int(x.replace(",", "").strip())
        return int(x)
    except Exception:
        return None


def _extract_place_block(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Với type=place, SerpAPI thường trả:
      - data["place_results"] (dict)  hoặc
      - fallback: data["local_results"][0] (dict)
    """
    if isinstance(data.get("place_results"), dict):
        return data["place_results"]
    lr = data.get("local_results")
    if isinstance(lr, list) and lr and isinstance(lr[0], dict):
        return lr[0]
    return data


def _signals_from_place_dict(d: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    # rating
    rating = None
    try:
        if d.get("rating") is not None:
            rating = float(d["rating"])
    except Exception:
        rating = None

    # total reviews
    total = _to_int(d.get("user_ratings_total") or d.get("reviews") or d.get("reviews_count"))
    return rating, total


def _place_params(
    *,
    place_id: str,
    name: str,
    lat: float,
    lng: float,
    language: str,
    country: str,
) -> Dict[str, Any]:
    """Tham số chuẩn để lấy chi tiết 1 địa điểm (ổn định hơn type=search)."""
    return {
        "engine": "google_maps",
        "type": "place",
        "data_id": place_id,
        "place_id": place_id,
        "q": name,
        "ll": f"@{lat},{lng},14z",
        "hl": language or "vi",
        "gl": country or "vn",
    }


# ===== Public API =====
async def fetch_rank_signals_map(
    candidates: Sequence[PlaceCandidate],
    *,
    language: str = "vi",
    country: str = "vn",
    concurrency: int = 8, # số request SerpAPI chạy song song (trên asyncio) trong 1 lần enrich/fetch.
    client: Optional[SerpApiClient] = None,
) -> Dict[str, RankSignals]:
    """
    Trả về {place_id -> RankSignals(rating, user_ratings_total)}.
    - Chỉ gọi mạng khi seed thiếu ít nhất 1 trường.
    - Không raise lỗi; lỗi mạng sẽ fallback về giá trị đang có trong seed.
    """
    owns = client is None
    client = client or SerpApiClient()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _resolve(c: PlaceCandidate) -> RankSignals:
        # đã đủ → không gọi mạng
        if (c.rating is not None) and (c.user_ratings_total is not None):
            return RankSignals(c.place_id, c.rating, c.user_ratings_total)

        # thiếu → gọi type=place để điền
        params = _place_params(
            place_id=c.place_id, name=c.name, lat=c.lat, lng=c.lng,
            language=language, country=country
        )
        try:
            async with sem:
                data = await client.get_json(params)
            blk = _extract_place_block(data)
            r, cnt = _signals_from_place_dict(blk)
            return RankSignals(
                place_id=c.place_id,
                rating=c.rating if r is None else r,
                user_ratings_total=c.user_ratings_total if cnt is None else cnt,
            )
        except Exception:
            # fallback: giữ nguyên seed
            return RankSignals(c.place_id, c.rating, c.user_ratings_total)

    try:
        tasks = [asyncio.create_task(_resolve(c)) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return {r.place_id: r for r in results}
    finally:
        if owns:
            await client.aclose()


async def light_enrich(
    candidates: Sequence[PlaceCandidate],
    *,
    language: str = "vi",
    country: str = "vn",
    concurrency: int = 8, # số request SerpAPI chạy song song (trên asyncio) trong 1 lần enrich/fetch.
    client: Optional[SerpApiClient] = None,
) -> List[PlaceCandidate]:
    """
    Enrich trực tiếp 2 trường còn thiếu vào PlaceCandidate: rating & user_ratings_total.
    """
    owns = client is None
    client = client or SerpApiClient()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _do(c: PlaceCandidate) -> PlaceCandidate:
        if (c.rating is not None) and (c.user_ratings_total is not None):
            return c
        params = _place_params(
            place_id=c.place_id, name=c.name, lat=c.lat, lng=c.lng,
            language=language, country=country
        )
        try:
            async with sem:
                data = await client.get_json(params)
            blk = _extract_place_block(data)
            r, cnt = _signals_from_place_dict(blk)
            if c.rating is None and r is not None:
                c.rating = r
            if c.user_ratings_total is None and cnt is not None:
                c.user_ratings_total = cnt
        except Exception:
            pass
        return c

    try:
        return list(await asyncio.gather(*[asyncio.create_task(_do(c)) for c in candidates]))
    finally:
        if owns:
            await client.aclose()


# Quick manual test
if __name__ == "__main__":
    import asyncio, os

    def fmt(x): return "-" if x is None else x

    async def _demo():
        from .nearby import nearby_search  # seeds chỉ có rating/reviews nếu SerpAPI trả ở type=search
        print("HAS_KEY?", bool(os.getenv("SERPAPI_KEY") or os.getenv("SERP_API_KEY")))

        seeds = await nearby_search(10.772, 106.698, tags=("restaurant",), page_size=8)
        print("Seeds:", len(seeds))
        # Thử fill bằng type=place cho mục còn thiếu
        sigs = await fetch_rank_signals_map(seeds)
        print("Signals (filled if missing):")
        missing = {"rating": 0, "reviews": 0}
        for s in seeds:
            g = sigs.get(s.place_id)
            if not g: 
                continue
            if g.rating is None:  missing["rating"] += 1
            if g.user_ratings_total is None: missing["reviews"] += 1
            print(f"- {s.name} | rating={fmt(g.rating)} | reviews={fmt(g.user_ratings_total)}")
        print("Missing summary:", missing)

    asyncio.run(_demo())
