from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

from app.schemas.place import PlaceCandidate
from app.schemas.ranking import (
    BayesianPrior,
    PlaceScored,
    RankSignals,
    WeightConfig,
)

# ----------------------- Small helpers -----------------------
# eta_score = exp * (- eta_s/tau_s) - The smaller the ETA, the higher the score.
TAU_S_DEFAULT = 900.0
def _eta_score(eta_s: float, tau_s: float = TAU_S_DEFAULT) -> float:
    # Normalize ETA to (0, 1].
    if eta_s is None or eta_s <= 0:
        return 0.0
    return float(math.exp(- float(eta_s) / float(tau_s)))


# Estimate speed (km/h) 
_SPEED_KPH = {
    "driving": 28.0,
    "motorcycling": 30.0,
    "walking": 6.0,
    "truck": 25.0,
}
# Estimate ETA from distance – fallback when no ETA from Matrix.
def _eta_from_distance(distance_m: Optional[int], mode: str = "driving") -> Optional[int]:
    if distance_m is None:
        return None
    v_kph = _SPEED_KPH.get(mode, _SPEED_KPH["driving"])
    v_mps = v_kph * 1000.0 / 3600.0
    if v_mps <= 0:
        return None
    return int(distance_m / v_mps)

"""Smooth rating using Bayesian – _bayesian_smooth_rating
Formula: r_smooth = (C * m + r * n) / (C + n)
C = 50 : prior strength (virtual review count)
m = 4.2: prior mean (global average rating)
r: original rating (0–5)
n: number of reviews (user_ratings_total)
"""
def _bayesian_smooth_rating(
    rating: Optional[float],
    n: Optional[int],
    prior: BayesianPrior,
) -> Optional[float]:
    if rating is None or n is None:
        return None
    C = max(0.0, float(prior.C))
    m = float(prior.m)
    return float((C * m + rating * n) / (C + n)) if (C + n) > 0 else None

# Normalize the number of reviews to the range [0, 1] using log-scale.
def _reviews_score(n: Optional[int], n_max: int) -> Optional[float]:
    if n is None or n <= 0 or n_max <= 0:
        return None
    return float(math.log10(n + 1) / math.log10(n_max + 1))

"""Formula (normalized):
score = sum( (w_i / sum_j w_j) * signal_i )
where signals = {eta_score, rating_score, reviews_score}
and default weights: w.eta = 0.5, w.rating = 0.3, w.reviews = 0.2.
If some signals are missing (None), the remaining weights are renormalized.
"""
def _reweight_and_combine(
    w: WeightConfig,
    eta_score: Optional[float],
    rating_score: Optional[float],
    reviews_score: Optional[float],
) -> Optional[float]:
    parts: List[Tuple[float, Optional[float]]] = [
        (w.eta, eta_score),
        (w.rating, rating_score),
        (w.reviews, reviews_score),
    ]
    parts = [(wi, si) for (wi, si) in parts if si is not None and wi > 0.0]
    if not parts:
        return None
    total_w = sum(wi for wi, _ in parts) or 1.0
    score = sum((wi / total_w) * float(si) for wi, si in parts)
    return float(score)


def _clone_to_scored(seed: PlaceCandidate) -> PlaceScored:
    """Sao chép PlaceCandidate → PlaceScored (giữ nguyên field POI)."""
    if hasattr(seed, "model_dump"):
        data = seed.model_dump()  # pydantic v2
    elif hasattr(seed, "dict"):
        data = seed.dict()        # pydantic v1 (legacy)
    else:
        data = seed.__dict__
    return PlaceScored(**data)

# ----------------------- Public API -----------------------

def build_scored_list(
    seeds: Iterable[PlaceCandidate],
    *,
    durations_s_by_id: Optional[Dict[str, int]] = None,
    distances_m_by_id: Optional[Dict[str, int]] = None,
    mode: str = "driving",
    weights: Optional[WeightConfig] = None,
    prior: Optional[BayesianPrior] = None,
    tau_s: float = TAU_S_DEFAULT,
) -> List[PlaceScored]:
    """
    **ETA-first strategy**:
      1) Nếu có ETA từ Matrix → dùng luôn (eta_source='matrix_duration').
      2) Nếu không có ETA, nhưng có distance:
         - distance từ Matrix → ước lượng ETA (eta_source='matrix_distance')
         - nếu không có, dùng distance_m trong seed (crow-fly) → (eta_source='seed_distance')
      3) Nếu không có cả distance lẫn ETA → signals.duration_s = None (score chỉ dựa rating/reviews).
    """
    weights = weights or WeightConfig()
    prior = prior or BayesianPrior()

    # Tìm max reviews để chuẩn hoá log-scale
    n_max = 0
    seeds_list = list(seeds)
    for s in seeds_list:
        n = getattr(s, "user_ratings_total", None)
        if isinstance(n, (int, float)):
            n_max = max(n_max, int(n))
    n_max = max(n_max, 20)

    out: List[PlaceScored] = []

    for s in seeds_list:
        sc = _clone_to_scored(s)
        sig = RankSignals()

        cid = getattr(s, "id", None)

        # ---- ETA-first ----
        dur_s: Optional[int] = None
        eta_src: Optional[str] = None

        # (1) ETA từ Matrix (nếu có)
        if durations_s_by_id and cid in durations_s_by_id:
            dur_s = durations_s_by_id[cid]
            eta_src = "matrix_duration"

        # Chuẩn bị distance cho fallback & display
        dist_m: Optional[int] = None
        dist_src: Optional[str] = None
        if distances_m_by_id and cid in distances_m_by_id:
            dist_m = distances_m_by_id[cid]
            dist_src = "matrix_distance"
        elif getattr(s, "distance_m", None) is not None:
            dist_m = getattr(s, "distance_m")
            dist_src = "seed_distance"

        # (2) Nếu chưa có ETA → ước lượng từ distance (ưu tiên matrix distance)
        if dur_s is None and dist_m is not None:
            dur_s = _eta_from_distance(dist_m, mode=mode)
            eta_src = dist_src

        sig.distance_m = dist_m if (isinstance(dist_m, (int, float)) and dist_m >= 0) else None
        sig.duration_s = dur_s if (isinstance(dur_s, (int, float)) and dur_s >= 0) else None
        sig.eta_source = eta_src
        sig.eta_score = _eta_score(sig.duration_s, tau_s) if sig.duration_s is not None else None

        # ---- Rating & Reviews ----
        r_raw = getattr(s, "rating", None)
        n_raw = getattr(s, "user_ratings_total", None)
        r_smooth = _bayesian_smooth_rating(r_raw, n_raw, prior)
        sig.rating = r_raw if isinstance(r_raw, (int, float)) else None
        sig.user_ratings_total = n_raw if isinstance(n_raw, (int, float)) else None
        sig.rating_score = (r_smooth / 5.0) if (r_smooth is not None) else None
        sig.reviews_score = _reviews_score(sig.user_ratings_total, n_max)

        # ---- Final score ----
        sig.score = _reweight_and_combine(weights, sig.eta_score, sig.rating_score, sig.reviews_score)

        sc.signals = sig
        out.append(sc)

    return out


def rank_and_cut(
    seeds: Iterable[PlaceCandidate],
    *,
    durations_s_by_id: Optional[Dict[str, int]] = None,
    distances_m_by_id: Optional[Dict[str, int]] = None,
    mode: str = "driving",
    weights: Optional[WeightConfig] = None,
    prior: Optional[BayesianPrior] = None,
    tau_s: float = TAU_S_DEFAULT,
    limit: int = 20,
) -> List[PlaceScored]:
    """
    Chấm điểm, sắp xếp và cắt danh sách kết quả.
    Tie-break (khi score bằng nhau) **ưu tiên ETA**:
      score ↓ ; ETA ↑ ; distance ↑ ; reviews ↓ ; rating ↓
    """
    scored = build_scored_list(
        seeds,
        durations_s_by_id=durations_s_by_id,
        distances_m_by_id=distances_m_by_id,
        mode=mode,
        weights=weights,
        prior=prior,
        tau_s=tau_s,
    )

    def _safe_num(x, default_big=False, negative=False):
        if x is None:
            v = 10**12 if default_big else 0
        else:
            v = float(x)
        return -v if negative else v

    # Sort: score ↓ ; ETA ↑ ; distance ↑ ; reviews ↓ ; rating ↓
    scored.sort(
        key=lambda c: (
            _safe_num(c.signals.score, negative=True),             # score cao → trước
            _safe_num(c.signals.duration_s, default_big=True),      # ETA nhỏ → trước (ưu tiên)
            _safe_num(c.signals.distance_m, default_big=True),      # distance nhỏ → trước
            _safe_num(c.signals.user_ratings_total, negative=True), # reviews nhiều → trước
            _safe_num(c.signals.rating, negative=True),             # rating cao → trước
        )
    )

    if limit and limit > 0:
        scored = scored[:limit]
    return scored
