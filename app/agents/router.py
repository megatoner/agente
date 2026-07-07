from fastapi import APIRouter, Depends, HTTPException, Header
from typing import Optional, List, Dict, Any
from app.agents.executor import run_agent
from app.agents.providers import list_available_models, get_llm
from app.agents.schemas import (
    ExecuteRequest, ExecuteResponse,
    RunRequest, RunResponse,
    ToolInfo, ModelInfo, HealthResponse,
)
from app.config import get_settings
from app.odoo.tools import create_odoo_tools
from sqlalchemy.orm import Session
from app.dependencies import get_db
import logging

router = APIRouter(prefix="/v1/agents", tags=["agents"])
settings = get_settings()
logger = logging.getLogger(__name__)


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="API Key inválida o faltante")
    return x_api_key


@router.post("/execute", response_model=ExecuteResponse)
def execute_agent(
    req: ExecuteRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Legacy endpoint. Use /run for dynamic agent configuration."""
    try:
        result = run_agent(
            message=req.message,
            model=req.model,
            session_id=req.session_id,
            db=db
        )
        return ExecuteResponse(**result)
    except Exception as e:
        logger.exception("Error en execute_agent")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run", response_model=RunResponse)
def run_agent_endpoint(
    req: RunRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Run an agent with full dynamic configuration from Odoo."""
    try:
        result = run_agent(
            message=req.message,
            model=req.agent_config.model,
            session_id=req.session_id,
            db=db,
            agent_config={
                "model": req.agent_config.model,
                "system_prompt": req.agent_config.system_prompt,
                "tools": req.agent_config.tools,
                "temperature": req.agent_config.temperature,
                "max_iterations": req.agent_config.max_iterations,
                "memory_enabled": req.agent_config.memory_enabled,
            },
            odoo_context=req.odoo_context,
            image_content=req.image_content,
        )
        return RunResponse(**result)
    except Exception as e:
        logger.exception("Error en run_agent_endpoint")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools", response_model=List[ToolInfo])
def get_tools(api_key: str = Depends(verify_api_key)):
    """List all available Odoo tools."""
    tools = create_odoo_tools()
    result = []
    for tool in tools:
        info = ToolInfo(
            name=tool.name,
            description=tool.description or "",
        )
        result.append(info)
    return result


@router.get("/models")
def get_models(api_key: str = Depends(verify_api_key)):
    """List available LLM models grouped by provider."""
    return list_available_models()


@router.get("/health", response_model=HealthResponse)
def health():
    """Extended health check for the agents server."""
    tools = create_odoo_tools()
    models_data = list_available_models()
    total_models = sum(len(v) for v in models_data.values())
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        models_loaded=total_models,
        tools_loaded=len(tools),
    )
