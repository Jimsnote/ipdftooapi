from fastapi import APIRouter, HTTPException

from app.config import settings
from app.core.logger import get_logger
from app.models.survey import SurveySubmission
from app.services.survey import SurveyStore

router = APIRouter()
logger = get_logger(__name__)
DB_PATH = settings.SURVEY_DB_PATH


@router.post("/submit")
def submit_survey(payload: SurveySubmission):
    if payload.website or payload.elapsed_ms < 3000:
        return {"status": "accepted"}

    try:
        SurveyStore(DB_PATH).save(payload)
    except Exception:
        logger.error("问卷写入失败", exc_info=False)
        raise HTTPException(status_code=500, detail="问卷提交失败，请稍后重试")
    return {"status": "accepted"}
