from __future__ import annotations

from typing import Optional, Literal
from pydantic import BaseModel, Field

from .place import PlaceCandidate


class WeightConfig(BaseModel):
    """
    Trọng số dùng khi trộn điểm (MVP không có price/open_now).
    - eta:     điểm từ ETA / (distance fallback)
    - rating:  điểm từ rating đã làm mượt (Bayesian)
    - reviews: điểm từ total reviews (log scaled)
    """
    eta: float = Field(default=0.36, ge=0.0)
    rating: float = Field(default=0.31, ge=0.0)
    reviews: float = Field(default=0.33, ge=0.0)

    @property
    def total(self) -> float:
        return self.eta + self.rating + self.reviews

    def normalized(self) -> "WeightConfig":
        """Trả về bản sao đã chuẩn hoá tổng = 1 (tránh lệch khi thiếu component)."""
        s = self.total or 1.0
        return WeightConfig(eta=self.eta / s, rating=self.rating / s, reviews=self.reviews / s)


class BayesianPrior(BaseModel):
    """
    Tham số làm mượt rating theo Bayesian:
      r_smooth = (C*m + rating*n) / (C + n)
    - m: prior mean (gợi ý 4.2)
    - C: độ tin cậy của prior (gợi ý 50)
    """
    m: float = Field(default=4.2)
    C: float = Field(default=50.0, ge=0.0)


class RankSignals(BaseModel):
    """
    Tập tín hiệu dùng cho Ranking (raw + normalized).
    - duration_s / distance_m: từ Matrix (ETA) hoặc fallback theo khoảng cách
    - rating / user_ratings_total: từ provider (đã enrich)
    - *_score: các điểm chuẩn hoá về [0,1]
    - score: điểm cuối sau khi trộn theo WeightConfig
    """
    # --- raw ---
    duration_s: Optional[int] = Field(default=None, ge=0)        # ETA (s), có thể None nếu fallback
    distance_m: Optional[int] = Field(default=None, ge=0)        # khoảng cách (m)
    rating: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    user_ratings_total: Optional[int] = Field(default=None, ge=0)

    # nguồn ETA để debug/explain
    eta_source: Optional[Literal["matrix", "distance_fallback"]] = None

    # --- normalized scores (0..1) ---
    eta_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rating_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reviews_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # --- final ---
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PlaceScored(PlaceCandidate):
    """
    Ứng viên đã được chấm điểm:
    - Kế thừa toàn bộ thuộc tính của PlaceCandidate (id, name, location, ...).
    - Gắn thêm 'signals' chứa ETA/distance, rating, reviews và điểm đã chuẩn hoá.
    """
    signals: RankSignals = Field(default_factory=RankSignals)


__all__ = ["WeightConfig", "BayesianPrior", "RankSignals", "PlaceScored"]
