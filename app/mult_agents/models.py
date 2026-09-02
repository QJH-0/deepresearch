"""动态模型工厂：按 config.json node_models 配置为每个节点绑定模型实例。

G5：config.json 节点级模型映射，默认 ChatTongyi(qwen)。
支持 OpenAI 兼容 API（DeepSeek 等）通过 base_url + api_key 注入。
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from langchain_community.chat_models import ChatTongyi
from langchain.agents import create_agent

from .config import AppConfig
from .prompts import PROMPTS
from .rag.core import RAGConfig
from .tools import init_rag_system
from .runtime import AgentBundle

logger = logging.getLogger("mult_agents")


def build_agent(model: str, api_key: str, prompt_key: str, temperature: float, tools: list):
    """构建单个 Agent（ChatTongyi + prompt + tools）。"""
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key
    llm = ChatTongyi(model=model, temperature=temperature)
    prompt = PROMPTS[prompt_key]
    return create_agent(model=llm, tools=tools, system_prompt=prompt)


def build_agents(model: str, api_key: str, config: AppConfig) -> AgentBundle:
    """构建全部节点 Agent。

    P1-3: 支持从 config.json 的 node_models 字段按节点配模型。
    未配置的节点使用默认 model。
    """
    rag_config = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name=config.milvus_collection,
    )
    init_rag_system(api_key=api_key, config=rag_config)

    # node_models 配置示例：
    # {"plan": {"model": "qwen-plus"}, "compress": {"model": "qwen-turbo"}}
    node_models = getattr(config, "node_models", None) or {}

    def _model_for(node_key: str, default_temp: float):
        """获取节点级模型配置，回退到默认 model。"""
        node_cfg = node_models.get(node_key, {})
        node_model = node_cfg.get("model", model)
        node_temp = node_cfg.get("temperature", default_temp)
        return build_agent(node_model, api_key, node_key, node_temp, [])

    return AgentBundle(
        intent_router=_model_for("intent_router", 0.0),
        planner=_model_for("plan", 0.3),
        scout_web=_model_for("web_search", 0.4),
        scout_local=_model_for("local_rag", 0.4),
        evidence_judge=_model_for("deep_dive", 0.2),
        analyst=_model_for("analyze", 0.3),
        direct_responder=_model_for("direct_answer", 0.2),
        writer=_model_for("write", 0.4),
    )
