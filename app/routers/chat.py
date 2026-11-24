"""질의응답 API (PRD F6, F7). CHAT_MODEL 이 없으면 LLM 없이 검색 + 템플릿으로 답한다."""
import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..config import Settings
from ..deps import get_db, get_embedder_or_none, get_settings_dep, get_vector_store
from ..services.mms import MMS_MAX_BYTES, build_mms, mms_bytes
from ..services.search import row_summary, search
from ..vector_store import VectorStore

router = APIRouter(prefix="/api", tags=["chat"])
log = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    question: str


def _polish_with_llm(settings: Settings, text: str) -> str:
    """선택 기능: CHAT_MODEL 이 설정됐을 때만 문구를 다듬는다. 실패하거나 사실이 바뀔 위험이 있으면 원문."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.chat_model,
            messages=[
                {"role": "system", "content": (
                    "고객에게 보낼 배차 안내 문자를 자연스럽게 다듬어라. 번호·날짜·톤수·상태·안내 문구는 "
                    "한 글자도 바꾸거나 빼지 말고, 새로운 정보를 추가하지 마라. 다듬은 문자 본문만 출력하라.")},
                {"role": "user", "content": text},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        # 숫자 토큰이 하나라도 사라지면 원문 유지
        import re

        if not out or set(re.findall(r"[A-Z]*\d[\d.\-]*", text)) - set(re.findall(r"[A-Z]*\d[\d.\-]*", out)):
            return text
        return out if mms_bytes(out) <= MMS_MAX_BYTES else text
    except Exception as e:
        log.warning("챗 모델 다듬기 실패, 템플릿 사용: %s", type(e).__name__)
        return text


@router.post("/chat")
def chat(req: ChatRequest, request: Request, settings: Settings = Depends(get_settings_dep),
         conn: sqlite3.Connection = Depends(get_db), vs: VectorStore = Depends(get_vector_store)):
    """질의 → 상태 표 + 고객사별 MMS 문구."""
    q = req.question.strip()
    if not q:
        return {"question": q, "match_type": "none", "found": False, "message": "질문을 입력해 주세요.",
                "rows": [], "messages": []}
    result = search(conn, vs, get_embedder_or_none(request), q)
    rows = [row_summary(r) for r in result.rows]
    if not rows:
        return {"question": q, "match_type": "none", "found": False, "rows": [], "messages": [],
                "message": "해당 제품을 찾을 수 없습니다."}

    messages = build_mms(rows)
    if settings.chat_model:
        for m in messages:
            m["text"] = _polish_with_llm(settings, m["text"])
            m["bytes"] = mms_bytes(m["text"])

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "question": q,
        "match_type": result.match_type,
        "matched": result.matched,
        "found": True,
        "message": f"{len(rows)}건을 찾았습니다.",
        "status_counts": counts,
        "rows": rows,
        "messages": messages,
        "llm_used": bool(settings.chat_model),
    }
