"""管理端点：配置热更新与查看。"""

import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from backend.config.settings import (
    AppSettings,
    BusinessSettings,
    _diff_restart_required,
    _RESTART_REQUIRED_FIELDS,
    get_business_settings,
    _set_business_settings,
    MiddlewareSettings,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_JSON_PATH = _PROJECT_ROOT / "config.json"


def _check_admin_token(x_admin_token: str | None) -> None:
    """校验管理端令牌。

    仅 development 环境保留「未配置 ADMIN_TOKEN 即放行」的本地开发便利；
    其他环境一律 fail-closed，避免部署后管理端点裸奔。
    """
    expected = MiddlewareSettings().admin_token
    if expected:
        if x_admin_token != expected:
            raise HTTPException(status_code=401, detail="invalid admin token")
        return
    if AppSettings().app_env != "development":
        raise HTTPException(
            status_code=401,
            detail="ADMIN_TOKEN 未配置，管理端点在非 development 环境不可用",
        )


@router.get("/config")
async def get_config(x_admin_token: str | None = Header(None)):
    """当前生效业务配置。"""
    _check_admin_token(x_admin_token)
    return get_business_settings().model_dump()


@router.post("/config/reload")
async def reload_config(x_admin_token: str | None = Header(None)):
    """重读磁盘 config.json 并原子生效。校验失败返回 422。"""
    _check_admin_token(x_admin_token)

    try:
        raw = json.loads(_CONFIG_JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(422, detail={"message": "config.json 解析失败", "error": str(e)})
    except Exception as e:
        raise HTTPException(422, detail={"message": f"config.json 读取失败: {e}"})

    business_fields = set(BusinessSettings.model_fields.keys())
    filtered = {k: v for k, v in raw.items() if k in business_fields}

    old = get_business_settings()
    try:
        new_biz = BusinessSettings(**filtered)
    except Exception as e:
        errors = []
        if hasattr(e, "errors"):
            errors = e.errors()
        raise HTTPException(422, detail={"message": "config.json 校验失败", "errors": errors})

    _set_business_settings(new_biz)
    restart_required = _diff_restart_required(old, new_biz)
    applied = [
        k for k in business_fields
        if getattr(old, k, None) != getattr(new_biz, k, None)
    ]
    return {
        "reloaded": True,
        "restart_required_fields": restart_required,
        "applied_fields": applied,
    }
