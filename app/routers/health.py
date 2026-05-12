from fastapi import APIRouter

router = APIRouter()


@router.get("/health", summary="Health check")
async def health_check():
    return {"status": "ok", "service": "ipdftoo-api"}
