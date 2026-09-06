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
from langchain_openai import ChatOpenAI

from .config import AppConfig
from .prompts import PROMPTS
from .rag.core import RAGConfig
from .tools import init_rag_system
from .runtime import AgentBundle

logger = logging.getLogger("mult_agents")


def build_agent(model: str, api_key: str, prompt_key: str, temperature: float, tools: list, enable_thinking: bool = False):
    """构建单个 Agent。"""
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key
    prompt = PROMPTS[prompt_key]

    if enable_thinking:
        llm = ChatOpenAI(
            api_key=api_key or os.getenv("DASHSCOPE_API_KEY", ""),
            model=model,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=temperature,
            extra_body={"enable_thinking": True},
        )
    else:
        llm = ChatTongyi(model=model, temperature=temperature)

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

    def _model_for(node_key: str, default_temp: float, enable_thinking: bool = False):
        """获取节点级模型配置，回退到默认 model。"""
        node_cfg = node_models.get(node_key, {})
        node_model = node_cfg.get("model", model)
        node_temp = node_cfg.get("temperature", default_temp)
        return build_agent(node_model, api_key, node_key, node_temp, [], enable_thinking=enable_thinking)

    thinking_nodes = set(getattr(config, "thinking_nodes", None) or [])
    return AgentBundle(
        intent_router=_model_for("intent_router", 0.0),
        planner=_model_for("plan", 0.3),
        scout_web=_model_for("web_search", 0.4),
        scout_local=_model_for("local_rag", 0.4),
        evidence_judge=_model_for("deep_dive", 0.2, enable_thinking="deep_dive" in thinking_nodes),
        analyst=_model_for("analyze", 0.3, enable_thinking="analyze" in thinking_nodes),
        direct_responder=_model_for("direct_answer", 0.2),
        writer=_model_for("write", 0.4, enable_thinking="write" in thinking_nodes),
        clarifier=build_agent("qwen-turbo", api_key, "clarify", 0.0, []),
    )
