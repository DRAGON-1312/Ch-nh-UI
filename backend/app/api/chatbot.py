from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter, HTTPException

from app.schemas.chatbot import ChatbotRequest, ChatbotResponse
from app.services.chatbot import handle_chat

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.get("/ping", summary="Healthcheck for chatbot")
async def ping() -> dict:
    return {"ok": True}


@router.post(
    "/ask",
    response_model=ChatbotResponse,
    response_model_exclude_none=True,  # NEW: ẩn field None cho JSON gọn
    summary="Chat with NestFeast bot",
    description="Nhận ChatbotRequest (message, history, context) và trả về ChatbotResponse.",
)
async def ask(req: ChatbotRequest) -> ChatbotResponse:
    try:
        # NEW: timeout cứng để tránh treo khi provider chậm
        async with asyncio.timeout(20):
            return await handle_chat(req)
    except TimeoutError:
        # NEW: phân biệt timeout để client xử lý retry/backoff
        raise HTTPException(status_code=504, detail="chatbot_timeout")
    except HTTPException:
        raise
    except Exception:
        # Giữ log đầy đủ server-side, trả mã lỗi chung ra ngoài
        log.exception("chatbot_error")
        raise HTTPException(status_code=500, detail="chatbot_error")
