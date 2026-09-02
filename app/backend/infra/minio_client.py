"""
MinIO 对象存储客户端 — 文档上传与下载。

参考:
  - RAGFlow (89.6k star): 使用 MinIO 存储所有上传文件，通过 STORAGE_IMPL 切换 MinIO/S3
  - Dify: 文档上传后存储到对象存储，DB 仅保存 storage_key 引用
  - minio Python SDK: https://min.io/docs/python/java/api-reference/index.html

设计要点:
  1. 上传文件返回 ObjectKey（唯一标识），不返回文件路径
  2. bucket 自动创建（if_not_exists）
  3. 支持通过 ObjectKey 下载文件用于解析
"""

import hashlib
import logging
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger("backend.infra.minio")


class MinIOStorage:
    """MinIO 对象存储封装。"""

    def __init__(
        self,
        endpoint: str = "localhost:9900",
        access_key: str = "minioadmin",
        secret_key: str = "minioadmin",
        bucket: str = "deep-research-docs",
        secure: bool = False,
    ):
        self._endpoint = endpoint
        self._bucket = bucket
        self._secure = secure
        self._client = Minio(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """启动时自动创建 bucket（如不存在）。"""
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("MinIO bucket 创建: %s", self._bucket)
            else:
                logger.info("MinIO bucket 已存在: %s", self._bucket)
        except S3Error as exc:
            logger.error("MinIO bucket 创建/检查失败: %s", exc)
            raise

    def upload_file(
        self,
        file_content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        上传文件到 MinIO，返回 ObjectKey。

        ObjectKey 格式: {date}/{uuid}_{filename}
        这样同一文件多次上传不会覆盖，且按日期便于管理。
        """
        date_str = datetime.utcnow().strftime("%Y%m%d")
        safe_name = Path(filename).name
        object_key = f"{date_str}/{uuid.uuid4().hex[:12]}_{safe_name}"

        self._client.put_object(
            bucket_name=self._bucket,
            object_name=object_key,
            data=BytesIO(file_content),
            length=len(file_content),
            content_type=content_type,
        )
        logger.info(
            "MinIO 上传成功 | bucket=%s | object_key=%s | size=%d bytes",
            self._bucket, object_key, len(file_content),
        )
        return object_key

    def download_file(self, object_key: str) -> bytes:
        """通过 ObjectKey 下载文件内容。"""
        response = self._client.get_object(self._bucket, object_key)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        logger.info("MinIO 下载完成 | object_key=%s | size=%d bytes", object_key, len(data))
        return data

    def delete_file(self, object_key: str) -> bool:
        """通过 ObjectKey 删除文件。"""
        try:
            self._client.remove_object(self._bucket, object_key)
            logger.info("MinIO 删除成功 | object_key=%s", object_key)
            return True
        except S3Error as exc:
            logger.error("MinIO 删除失败 | object_key=%s | error=%s", object_key, exc)
            return False

    def file_exists(self, object_key: str) -> bool:
        """检查文件是否存在。"""
        try:
            self._client.stat_object(self._bucket, object_key)
            return True
        except S3Error:
            return False
