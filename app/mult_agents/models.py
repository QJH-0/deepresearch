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
from pydantic import BaseModel

from .config import AppConfig
from .output_schemas import IntentDecision
from .prompts import PROMPTS
from .rag.core import RAGConfig
from .tools import init_rag_system
from .runtime import AgentBundle

logger = logging.getLogger("mult_agents")


def _build_llm(
    model: str,
    api_key: str,
    temperature: float,
    *,
    timeout: float,
    max_retries: int,
    enable_thinking: bool = False,
):
    """构建底层对话模型。"""
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key

    if enable_thinking:
        return ChatOpenAI(
            api_key=api_key or os.getenv("DASHSCOPE_API_KEY", ""),
            model=model,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            extra_body={"enable_thinking": True},
        )

    # streaming=True 是 token 级流式的前提：ChatTongyi 默认 False 时，
    # astream(stream_mode="messages") 只产出一个整块响应，
    # 前端表现为「答案一大段直接吐出」而不是打字机式输出。
    # ChatTongyi 没有 timeout 字段，只能经 model_kwargs 透传 dashscope 的 request_timeout；
    # 其 max_retries 默认为 10，是长尾请求的真实来源，统一由配置收窄。
    return ChatTongyi(
        model=model,
        temperature=temperature,
        streaming=True,
        max_retries=max_retries,
        model_kwargs={"request_timeout": timeout},
    )


def build_agent(
    model: str,
    api_key: str,
    prompt_key: str,
    temperature: float,
    tools: list,
    *,
    timeout: float,
    max_retries: int,
    enable_thinking: bool = False,
):
    """构建单个 Agent。

    timeout / max_retries 为必填关键字参数：不给默认值是为了强制调用方从 config 取值，
    避免模型层自带默认值与配置分叉后各说各话。
    """
    llm = _build_llm(
        model,
        api_key,
        temperature,
        timeout=timeout,
        max_retries=max_retries,
        enable_thinking=enable_thinking,
    )
    return create_agent(model=llm, tools=tools, system_prompt=PROMPTS[prompt_key])


@dataclass(frozen=True)
class StructuredAgent:
    """决策节点的结构化执行体。

    不走 create_agent：langchain 1.x 的 response_format 依赖强制 tool_choice，
    而 DashScope 只接受 none/auto，实测报 InvalidParameter。改为直接绑定 schema，
    由模型以工具调用形式产出，调用方再按 schema 校验。
    """

    runnable: object
    system_prompt: str
    schema: type[BaseModel]


def build_structured_agent(
    model: str,
    api_key: str,
    prompt_key: str,
    temperature: float,
    *,
    timeout: float,
    max_retries: int,
    response_format: type[BaseModel],
) -> StructuredAgent:
    """构建决策节点的结构化执行体。"""
    llm = _build_llm(model, api_key, temperature, timeout=timeout, max_retries=max_retries)
    return StructuredAgent(
        runnable=llm.bind_tools([response_format]),
        system_prompt=PROMPTS[prompt_key],
        schema=response_format,
    )


def build_agents(model: str, api_key: str, config: AppConfig) -> AgentBundle:
    """构建全部节点 Agent。

    P1-3: 支持从 config.json 的 node_models 字段按节点配模型。
    未配置的节点使用默认 model。
    """
    # collection 名走 RAGConfig 默认常量（rag.core 中的单一事实源），
    # 不再从 config.milvus_collection 取 —— 该配置项指向的集合与
    # app_main / document_service 使用的集合不一致，会造成写入与检索分叉。
    # postgres_dsn 用于启用 PG 关键词召回。
    rag_config = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        postgres_dsn=config.postgres_dsn,
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
        return build_agent(
            node_model, api_key, node_key, node_temp, [],
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            enable_thinking=enable_thinking,
        )

    def _structured_for(node_key: str, default_temp: float, response_format):
        """决策节点的模型配置解析，与 _model_for 同源。"""
        node_cfg = node_models.get(node_key, {})
        return build_structured_agent(
            node_cfg.get("model", model),
            api_key,
            node_key,
            node_cfg.get("temperature", default_temp),
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            response_format=response_format,
        )

    thinking_nodes = set(getattr(config, "thinking_nodes", None) or [])
    return AgentBundle(
        intent_router=_structured_for("intent_router", 0.0, IntentDecision),
        planner=_model_for("plan", 0.3),
        scout_web=_model_for("web_search", 0.4),
        scout_local=_model_for("local_rag", 0.4),
        evidence_judge=_model_for("deep_dive", 0.2, enable_thinking="deep_dive" in thinking_nodes),
        analyst=_model_for("analyze", 0.3, enable_thinking="analyze" in thinking_nodes),
        direct_responder=_model_for("direct_answer", 0.2),
        writer=_model_for("write", 0.4, enable_thinking="write" in thinking_nodes),
        clarifier=build_agent(
            "qwen-turbo", api_key, "clarify", 0.0, [],
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
        ),
    )
