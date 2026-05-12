import os
from typing import Optional

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)


class StorageService:
    """
    Tencent Cloud COS Storage Service wrapper.

    TODO: Implement actual COS integration when credentials are available.
    For now, this is a placeholder that stores files locally.
    """

    def __init__(self):
        self.enabled = bool(settings.COS_SECRET_ID and settings.COS_BUCKET)
        if self.enabled:
            try:
                from qcloud_cos import CosConfig, CosS3Client

                config = CosConfig(
                    Region=settings.COS_REGION,
                    SecretId=settings.COS_SECRET_ID,
                    SecretKey=settings.COS_SECRET_KEY,
                )
                self.client = CosS3Client(config)
                self.bucket = settings.COS_BUCKET
                logger.info(f"COS storage initialized: {self.bucket}")
            except Exception as e:
                logger.warning(f"Failed to initialize COS: {e}")
                self.enabled = False
        else:
            logger.info("COS not configured, using local storage")

    def upload_and_get_url(self, local_path: str, key: str) -> str:
        if not self.enabled:
            # Return local path for development
            return local_path

        # Upload to COS
        self.client.upload_file(
            Bucket=self.bucket,
            Key=key,
            LocalFilePath=local_path,
            EnableMD5=False,
        )

        # Generate presigned URL
        url = self.client.get_presigned_url(
            Method="GET",
            Bucket=self.bucket,
            Key=key,
            Expired=settings.COS_UPLOAD_EXPIRE,
        )
        logger.info(f"Uploaded to COS: {key}")
        return url

    def delete(self, key: str):
        if not self.enabled:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"Deleted from COS: {key}")
        except Exception as e:
            logger.error(f"Failed to delete {key}: {e}")
