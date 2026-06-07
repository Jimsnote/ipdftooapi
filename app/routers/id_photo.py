import os
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.logger import get_logger
from app.schemas.id_photo import IDPhotoRenderRequest
from app.models.schemas import TaskResponse
from app.services.id_photo_renderer import IDPhotoRendererService

logger = get_logger(__name__)
router = APIRouter()

TEMP_DIR = "/app/temp" if os.path.exists("/app/temp") else "./temp"
os.makedirs(TEMP_DIR, exist_ok=True)



@router.post("/render", response_model=TaskResponse, summary="Render A4 ID photo layout to PDF")
async def render_id_photo(payload: IDPhotoRenderRequest):
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    output_path = os.path.join(task_dir, "id_photo_layout.pdf")

    try:
        IDPhotoRendererService.render_to_file(payload, output_path)
    except Exception as e:
        logger.error(f"ID photo render task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=f"渲染失败: {e}")

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
    task_dir = os.path.join(TEMP_DIR, task_id)
    output_path = os.path.join(task_dir, "id_photo_layout.pdf")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename="id_photo_layout.pdf",
    )
