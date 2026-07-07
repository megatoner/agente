"""Pydantic schemas for the agents API."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class ExecuteRequest(BaseModel):
    message: str
    model: Optional[str] = None
    session_id: Optional[str] = "default"


class ExecuteResponse(BaseModel):
    output: str
    session_id: Optional[str] = None
    model_used: str


class ToolConfig(BaseModel):
    name: str
    enabled: bool = True
    description: Optional[str] = None


class AgentConfig(BaseModel):
    model: str = Field(..., description="LLM model string, e.g. 'openai/gpt-4o-mini'")
    system_prompt: Optional[str] = "You are a helpful assistant."
    tools: List[str] = Field(default_factory=list, description="List of tool names to enable")
    memory_enabled: bool = True
    temperature: float = 0.2
    max_iterations: int = 15


class RunRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message")
    agent_config: AgentConfig
    session_id: Optional[str] = "default"
    odoo_context: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Odoo context: db, uid, api_key, user_info")
    image_content: Optional[Dict[str, Any]] = Field(default=None, description="Imagen adjunta en formato {type, media_type, data}")


class RunResponse(BaseModel):
    output: str
    session_id: Optional[str] = None
    model_used: str
    tools_used: List[str] = Field(default_factory=list)
    iterations: int = 0
    usage: Optional[Dict[str, Any]] = None
    success: bool = True
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: Optional[Dict[str, Any]] = None


class ModelInfo(BaseModel):
    id: str
    provider: str
    name: str
    context_window: Optional[int] = None


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str = "1.1.0"
    models_loaded: int
    tools_loaded: int
