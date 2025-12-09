from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx
from pathlib import Path
from dotenv import load_dotenv
import random

# --- load .env ở project root và backend/ ---
_here = Path(__file__).resolve()
load_dotenv(_here.parents[4] / ".env")   # <repo>/.env
load_dotenv(_here.parents[3] / ".env")   # backend/.env


# ===================== Models đơn giản =====================

@dataclass
class ChatMessage:
    """Tin nhắn chung cho các LLM backend."""
    role: str   # "system" | "user" | "assistant"
    content: str

@dataclass
class GeminiChatResult:
    """Kết quả đã parse từ Gemini (dùng cho services.chatbot)."""
    text: str
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    raw: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ===================== Low-level client =====================

class GeminiClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
        retries: int = 5,
        backoff_base: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        self.model = (model or os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.backoff_base = backoff_base
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_s)

    # ---- helpers ----
    def _url(self, *, stream: bool = False) -> str:
        """Build REST endpoint cho generateContent/streamGenerateContent."""
        suffix = ":streamGenerateContent" if stream else ":generateContent"
        return f"{self.base_url}/models/{self.model}{suffix}"

    # Một số code cũ có thể gọi .url(), giữ alias để an toàn
    def url(self, *, stream: bool = False) -> str:  # pragma: no cover
        return self._url(stream=stream)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "GeminiClient":  # pragma: no cover
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        await self.aclose()

    async def generate_raw(self, payload: Dict[str, Any], *, stream: bool = False) -> Dict[str, Any]:
        if not self.api_key:
            return {"error": "missing_api_key"}
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        transient = {429, 500, 502, 503, 504}
        last: Optional[Dict[str, Any]] = None

        for i in range(self.retries + 1):
            try:
                if stream:
                    async with self.client.stream("POST", self._url(stream=True), headers=headers, json=payload) as r:
                        # Nếu lỗi HTTP → đọc body và trả lỗi chuẩn hoá
                        if r.status_code >= 400:
                            try:
                                data = await r.json()
                            except Exception:
                                data = {"message": (await r.aread()).decode(errors="ignore")[:500]}
                            err_obj = data.get("error", data)
                            last = {"error": f"http_{r.status_code}", "provider_error": err_obj}
                            if r.status_code in transient and i < self.retries:
                                delay = self.backoff_base * (2 ** i) + random.uniform(0, 0.4)
                                await asyncio.sleep(delay)
                                continue
                            return last
                        # Gom các chunk text lại (Gemini stream trả NDJSON/event-stream)
                        chunks = [c async for c in r.aiter_text()]
                        return {"_stream_text": "".join(chunks)}
                else:
                    resp = await self.client.post(self._url(stream=False), headers=headers, json=payload)
                    if resp.headers.get("content-type", "").startswith("application/json"):
                        data = resp.json()
                    else:
                        data = {"raw": await resp.aread()}

                    if resp.status_code < 400:
                        return data

                    # Lỗi: chuẩn hoá thông tin error
                    err_obj = data.get("error", data)
                    last = {"error": f"http_{resp.status_code}", "provider_error": err_obj}

                    # Retry nếu là lỗi tạm thời
                    if resp.status_code in transient and i < self.retries:
                        delay = self.backoff_base * (2 ** i) + random.uniform(0, 0.4)
                        await asyncio.sleep(delay)
                        continue

                    return last
            except httpx.RequestError as exc:
                last = {"error": "network", "detail": str(exc)}
                if i < self.retries:
                    delay = self.backoff_base * (2 ** i) + random.uniform(0, 0.4)
                    await asyncio.sleep(delay)
                    continue
                return last

        return last or {"error": "unknown"}


# ===================== Helper: messages -> payload =====================

def _messages_to_payload(
    messages: Sequence[Union[ChatMessage, Dict[str, str]]],
    *,
    temperature: float = 0.4,
    max_output_tokens: int = 3000,
    stop: Optional[List[str]] = None,
    response_mime_type: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    safety_settings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    sys_parts: List[Dict[str, str]] = []
    contents: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, ChatMessage):
            m = ChatMessage(role=str(m.get("role", "user")), content=str(m.get("content", "")))
        role = m.role.lower().strip()
        if role == "system":
            if m.content:
                sys_parts.append({"text": m.content})
            continue
        gemini_role = "user" if role in ("user", "client", "human") else "model"
        if m.content:
            contents.append({"role": gemini_role, "parts": [{"text": m.content}]})

    if not contents:
        raise ValueError("Gemini payload must have at least one non-system message with text.")

    payload: Dict[str, Any] = {"contents": contents}
    if sys_parts:
        payload["system_instruction"] = {"parts": sys_parts}

    gen: Dict[str, Any] = {
        "temperature": float(temperature),
        "maxOutputTokens": int(max_output_tokens),
    }
    if stop:
        gen["stopSequences"] = stop
    if response_mime_type:
        gen["responseMimeType"] = response_mime_type
    if response_schema:
        gen["responseSchema"] = response_schema

    payload["generationConfig"] = gen
    if tools:
        payload["tools"] = tools
    if safety_settings:
        payload["safetySettings"] = safety_settings
    return payload


# ===================== High-level API =====================

async def chat(
    messages: Sequence[Union[ChatMessage, Dict[str, str]]],
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_output_tokens: int = 3000,
    stop: Optional[List[str]] = None,
    response_mime_type: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    safety_settings: Optional[List[Dict[str, Any]]] = None,
    stream: bool = False,
    client: Optional[GeminiClient] = None,
) -> GeminiChatResult:
    """
    Nhận list messages → gọi Gemini → trả GeminiChatResult.
    Hỗ trợ JSON-mode (response_mime_type/response_schema), stop sequences, tools, safety.
    """
    payload = _messages_to_payload(
        messages,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        stop=stop,
        response_mime_type=response_mime_type,
        response_schema=response_schema,
        tools=tools,
        safety_settings=safety_settings,
    )

    owns = client is None
    client = client or GeminiClient(api_key=api_key, model=model)
    data = await client.generate_raw(payload, stream=stream)
    if owns:
        await client.aclose()

    # Lỗi HTTP/mạng/thiếu key
    if isinstance(data, dict) and data.get("error"):
        return GeminiChatResult(text="", error=str(data.get("error")), raw=data)

    # Parse text/usage
    text = ""
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    try:
        candidates = data.get("candidates") or []
        if candidates:
            c0 = candidates[0]
            finish_reason = c0.get("finishReason")
            parts = c0.get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
    except Exception:
        pass

    try:
        usage_meta = data.get("usageMetadata") or {}
        usage = {
            "prompt_tokens": int(usage_meta.get("promptTokenCount", 0)),
            "completion_tokens": int(usage_meta.get("candidatesTokenCount", 0)),
            "total_tokens": int(usage_meta.get("totalTokenCount", 0)),
        }
    except Exception:
        usage = None

    return GeminiChatResult(text=text, finish_reason=finish_reason, usage=usage, raw=data, error=None)


# ===================== Quick manual test =====================

if __name__ == "__main__":  # pragma: no cover
    import sys, argparse, asyncio

    # Re-load .env từ project root và backend (đề phòng chạy từ thư mục khác)
    for p in (_here.parents[4] / ".env", _here.parents[3] / ".env"):
        if p.exists():
            load_dotenv(p, override=False)

    # Windows: tránh warning deprecated ở 3.14+
    if os.name == "nt" and sys.version_info < (3, 14):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Quick Gemini sanity check")
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    parser.add_argument("--msg", default="Hãy trả về 1 câu ngắn để xác nhận đang hoạt động (tiếng Việt).")
    parser.add_argument("--max_tokens", type=int, default=64)
    args = parser.parse_args()

    async def _demo() -> int:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        print("HAS_KEY:", bool(api_key))
        print("MODEL:", args.model)
        if not api_key:
            print("→ Thiếu GEMINI_API_KEY/GOOGLE_API_KEY trong .env")
            return 2

        msgs = [ChatMessage("system", "Bạn là trợ lý NestFeast."), ChatMessage("user", args.msg)]
        res = await chat(msgs, max_output_tokens=args.max_tokens, model=args.model)
        if res.error:
            print("ERROR:", res.error, "\nRAW:", res.raw)
            return 3

        print("TEXT:", (res.text or "").strip())
        print("USAGE:", res.usage)
        return 0 if (res.text or "").strip() else 4

    sys.exit(asyncio.run(_demo()))
