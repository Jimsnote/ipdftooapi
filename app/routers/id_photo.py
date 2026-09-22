import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.logger import get_logger
from app.schemas.id_photo import IDPhotoRenderRequest
from app.models.schemas import TaskResponse
from app.services.id_photo_renderer import IDPhotoRendererService
from app.core.file_security import get_task_dir, make_task_dir, safe_join

logger = get_logger(__name__)
router = APIRouter()

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)



@router.post("/render", response_model=TaskResponse, summary="Render A4 ID photo layout to PDF")
async def render_id_photo(payload: IDPhotoRenderRequest):
    task_id, task_dir = make_task_dir(TEMP_DIR)
    output_path = safe_join(task_dir, "id_photo_layout.pdf")

    try:
        IDPhotoRendererService.render_to_file(payload, output_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"ID photo render task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail="渲染失败，请稍后重试")

    download_url = f"/api/v1/id-photo/download/{task_id}"
    return TaskResponse(
        task_id=task_id,
        status="completed",
        message="排版PDF生成成功",
        download_url=download_url,
        file_count=1,
    )


@router.get("/download/{task_id}", summary="Download rendered ID photo PDF")
async def download_id_photo(task_id: str):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    output_path = safe_join(task_dir, "id_photo_layout.pdf")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename="id_photo_layout.pdf",
    )
