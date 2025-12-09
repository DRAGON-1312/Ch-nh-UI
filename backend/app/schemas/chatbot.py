from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .location import NormalizedOrigin
from .common import Tag


# ========== Kiểu message cơ bản cho lịch sử hội thoại ==========


class ChatRole(str, Enum):
    """Vai trò của một message trong hội thoại."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessage(BaseModel):
    """
    Một tin nhắn trong lịch sử chat.

    - role: user / assistant / system
    - content: nội dung text
    """
    role: ChatRole = Field(..., description="Role của message: user/assistant/system.")
    content: str = Field(..., description="Nội dung tin nhắn.")


# ========== Ý định / hành động chính mà chatbot kích hoạt ==========


class ChatbotIntent(str, Enum):
    """
    Ý định lõi mà chatbot hiểu được từ câu hỏi của user.

    Dùng cho service + FE để quyết định hành động tiếp theo:
    - QUICK_RECOMMEND: bắt đầu flow tìm quán quanh đây.
    - CLARIFY_ORIGIN: hỏi lại khi vị trí xuất phát mơ hồ.
    - CHANGE_RADIUS: tăng / giảm bán kính.
    - CHANGE_MODE: đổi phương tiện (walking / motorcycling / driving...).
    - PICK_PLACE: chọn 1 quán trong danh sách hiện tại để xem nhanh (ETA/distance...).
    - DETAIL_PLACE: xin xem chi tiết 1 địa điểm (tóm tắt bằng Gemini/LLM hoặc rule-based).
    - NONE: trả lời thuần túy, không trigger hành động đặc biệt nào.
    """
    QUICK_RECOMMEND = "quick_recommend"
    CLARIFY_ORIGIN = "clarify_origin"
    CHANGE_RADIUS = "change_radius"
    CHANGE_MODE = "change_mode"
    PICK_PLACE = "pick_place"
    DETAIL_PLACE = "detail_place"
    NONE = "none"


# ========== Context NestFeast mà bot cần biết ==========


class ChatbotContext(BaseModel):
    """
    Ngữ cảnh tìm kiếm hiện tại quanh user mà chatbot nhìn thấy.

    Đây là “state dùng chung” giữa:
    - FE
    - chatbot
    - các service khác (intake, nearbySearch, routing...)
    """

    class Config:
        # Cho phép nhận thêm field lạ từ FE mà không bị lỗi (vd: version, model...)
        extra = "ignore"

    # ---- Vị trí xuất phát ----
    origin: Optional[NormalizedOrigin] = Field(
        default=None,
        description="Vị trí xuất phát đã được chuẩn hóa (nếu có).",
    )

    # Raw text mà user/FE gửi lên để intake_origin xử lý (dùng cho case origin mơ hồ)
    origin_text: Optional[str] = Field(
        default=None,
        description="Địa chỉ xuất phát dạng text gốc user nhập (chưa chuẩn hóa).",
    )

    # ---- Tham số tìm kiếm xung quanh ----
    radius_km: float = Field(
        default=3.0,
        ge=0,
        description="Bán kính tìm kiếm hiện tại (km).",
    )
    mode: str = Field(
        default="motorcycling",
        description="Phương tiện di chuyển (vd: 'walking', 'motorcycling', 'driving').",
    )
    tags: List[Tag] = Field(
        default_factory=list,
        description="Danh sách tag đang quan tâm (restaurant / cafe / hotel...).",
    )

    # Ngôn ngữ & quốc gia (để chọn prompt, text reply, provider...)
    lang: str = Field(
        default="vi",
        description="Ngôn ngữ hiển thị ưu tiên (vd: 'vi', 'en').",
    )
    country: str = Field(
        default="vn",
        description="Mã quốc gia (ISO 3166-1 alpha-2, vd: 'vn').",
    )

    # Để refine theo danh sách đã rank, giữ list place_id đang focus (nếu có).
    active_place_ids: List[str] = Field(
        default_factory=list,
        description="Danh sách place_id mà user đang xem / cần refine (top N).",
    )


# ========== Chip (quick reply) cho UI ==========


class ChatChip(BaseModel):
    """
    Quick reply / chip hiển thị dưới câu trả lời của bot.

    Ví dụ:
    - label: 'Tăng bán kính lên 5 km'
      intent: CHANGE_RADIUS
      payload: { 'radius_km': 5 }
    - label: 'Đổi sang cafe'
      intent: QUICK_RECOMMEND
      payload: { 'tags': ['cafe'] }

    Khi hỏi chi tiết 1 địa điểm:
    - intent: DETAIL_PLACE
      payload có thể là một trong:
        { 'index': 2 }            # chọn theo thứ hạng hiện tại (1-based)
        { 'place_id': '...' }     # chọn theo id
        { 'name': 'The Coffee...' }  # fallback theo tên (backend sẽ resolve)
    """

    label: str = Field(..., description="Text hiển thị trên chip.")
    intent: ChatbotIntent = Field(
        default=ChatbotIntent.NONE,
        description="Hành động gợi ý khi user bấm chip.",
    )
    payload: Dict[str, object] = Field(
        default_factory=dict,
        description="Tham số kèm theo (vd: radius_km mới, mode mới, place_id/index/name...).",
    )


# ========== Kiểu dữ liệu 'chi tiết địa điểm' cho phản hồi bot ==========


class PlaceDetailSummary(BaseModel):
    """
    Tóm tắt ngắn gọn về 1 địa điểm (có thể do LLM sinh ra hoặc rule-based).
    """
    # 1) Cho phép không có place_id trong trường hợp chỉ biết "name" (user gõ tự do)
    place_id: Optional[str] = Field(default=None, description="ID của địa điểm (provider-composite).")
    name: Optional[str] = Field(default=None, description="Tên địa điểm.")
    address: Optional[str] = Field(default=None, description="Địa chỉ (nếu có).")
    tags: List[Tag] = Field(default_factory=list, description="Tag/loại địa điểm.")
    rating: Optional[float] = Field(default=None, description="Điểm rating.")
    user_ratings_total: Optional[int] = Field(default=None, description="Số lượng đánh giá.")
    eta_s: Optional[int] = Field(default=None, description="ETA (giây) nếu có.")
    distance_m: Optional[int] = Field(default=None, description="Khoảng cách (m) nếu có.")
    summary: Optional[str] = Field(default=None, description="Tóm tắt nội dung chi tiết.")
    source: Optional[str] = Field(default=None, description="Nguồn tạo summary: 'gemini'/'rule'...")

    # 2) Mang theo usage để debug/hiển thị chi phí
    llm_usage: Optional[Dict[str, int]] = Field(
        default=None, description="Token usage từ LLM (prompt/completion/total)."
    )

    # ràng buộc: phải có ít nhất name hoặc place_id
    @model_validator(mode="after")
    def _require_id_or_name(self) -> "PlaceDetailSummary":
        if not (self.place_id or self.name):
            raise ValueError("PlaceDetailSummary cần ít nhất 'place_id' hoặc 'name'.")
        return self


# ========== Request / Response cho API chatbot ==========


class ChatbotRequest(BaseModel):
    """
    Payload FE gửi lên service chatbot.

    - message: câu hỏi mới của user (natural language).
    - history: vài lượt chat trước đó (nếu muốn giữ context, < 3 turn).
    - context: origin/radius/mode/tags/lang/country hiện tại bên FE.
    """
    message: str = Field(..., description="Tin nhắn mới của user.")
    history: List[ChatMessage] = Field(
        default_factory=list,
        description="Lịch sử chat gần nhất giữa user và bot.",
    )
    context: ChatbotContext = Field(
        default_factory=ChatbotContext,
        description="Context NestFeast hiện tại.",
    )


class ChatbotResponse(BaseModel):
    """
    Kết quả chatbot trả về cho FE.

    - reply: câu trả lời (tóm tắt ngắn + top3 nếu có).
    - intent: ý định lõi (để FE/service quyết định có chạy quick-recommend,
      đổi radius, đổi mode, pick place, hay hiển thị chi tiết).
    - chips: các quick-reply để user bấm tiếp (tăng/giảm radius, đổi mode, v.v.).
    - detail: nếu intent là DETAIL_PLACE, có thể đính kèm tóm tắt chi tiết tại đây
      (FE vẫn có thể chỉ dùng reply text nếu muốn đơn giản).
    - context: context sau khi bot đã hiểu và (có thể) cập nhật lại.
    """

    reply: str = Field(..., description="Câu trả lời chính của chatbot.")
    intent: ChatbotIntent = Field(
        default=ChatbotIntent.NONE,
        description="Ý định lõi mà bot suy ra từ câu hỏi.",
    )
    chips: List[ChatChip] = Field(
        default_factory=list,
        description="Danh sách quick-reply gợi ý cho user.",
    )
    detail: Optional[PlaceDetailSummary] = Field(
        default=None,
        description="Thông tin chi tiết 1 địa điểm (tóm tắt bởi LLM hoặc rule-based).",
    )
    context: ChatbotContext = Field(
        default_factory=ChatbotContext,
        description="Context sau khi bot xử lý.",
    )
