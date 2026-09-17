"""动态模型工厂：按 config.json 的 node_models 为每个节点绑定模型实例。

统一走 DashScope 的 OpenAI 兼容通道，不再混用原生 SDK 通道：
结构化输出依赖 response_format 的 json_schema 模式，原生 SDK 通道不支持；
兼容通道同时提供思考模式（enable_thinking）与流式。
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import AppConfig
from .output_schemas import AnalysisDraft, IntentDecision, PlanDraft, ReflectionDraft
from .prompts import PROMPTS
from .rag.core import RAGConfig
from .tools import init_rag_system
from .runtime import AgentBundle

logger = logging.getLogger("mult_agents")

_DASHSCOPE_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 支持 response_format={"type": "json_schema"} 的型号（百炼结构化输出文档）。
# 范围比 JSON Object 模式窄得多，结构化节点配错型号必须启动即失败，
# 否则会退化到运行期才报 schema 校验错误。
JSON_SCHEMA_MODELS = (
    "qwen3.7-plus",
    "qwen3.7-flash",
    "qwen3.7-max",
    "qwen3.8-flash",
    "qwen3.8-max",
)


def supports_json_schema(model: str) -> bool:
    """判断型号是否支持 JSON Schema 模式；带日期后缀的快照版本同样算支持。"""
    return any(model == name or model.startswith(f"{name}-") for name in JSON_SCHEMA_MODELS)


def _build_llm(
    model: str,
    api_key: str,
    temperature: float,
    *,
    timeout: float,
    max_retries: int,
    enable_thinking: bool = False,
):
    """构建底层对话模型。

    enable_thinking 始终显式下发：Qwen3.7-Flash 等型号默认开启思考，
    不显式关闭会让本应快速的判定节点（意图/规划）付出成倍的延迟与成本。
    """
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key

    return ChatOpenAI(
        api_key=api_key or os.getenv("DASHSCOPE_API_KEY", ""),
        model=model,
        base_url=_DASHSCOPE_COMPAT_BASE_URL,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        extra_body={"enable_thinking": enable_thinking},
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


def build_aux_llm(
    model: str,
    api_key: str,
    *,
    timeout: float,
    max_retries: int,
    temperature: float = 0.1,
):
    """辅助任务（会话标题 / 对话摘要 / 记忆抽取）用的轻量模型。

    与主链路共用同一客户端：原生 SDK 通道不支持 Qwen3.7/3.8 系列，
    继续用它会把辅助链路锁死在旧型号上。
    """
    return _build_llm(
        model,
        api_key,
        temperature,
        timeout=timeout,
        max_retries=max_retries,
        enable_thinking=False,
    )


@dataclass(frozen=True)
class StructuredAgent:
    """决策节点的结构化执行体。

    走 DashScope 的 JSON Schema 模式（response_format.type=json_schema + strict），
    由 provider 保证输出结构，调用方只需按 schema 反序列化。
    不走 create_agent：langchain 1.x 的 response_format 依赖强制 tool_choice，
    而 DashScope 只接受 none/auto，实测报 InvalidParameter。
    """

    runnable: object
    system_prompt: str
    schema: type[BaseModel]


def _json_schema_response_format(schema: type[BaseModel]) -> dict:
    """把 Pydantic 模型转成百炼 JSON Schema 模式的 response_format。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": schema.model_json_schema(),
        },
    }


def build_structured_agent(
    model: str,
    api_key: str,
    prompt_key: str,
    temperature: float,
    *,
    timeout: float,
    max_retries: int,
    response_format: type[BaseModel],
    enable_thinking: bool = False,
) -> StructuredAgent:
    """构建决策节点的结构化执行体。"""
    if not supports_json_schema(model):
        raise ValueError(
            f"型号 {model} 不支持 response_format 的 json_schema 模式，"
            f"无法用于结构化节点 {prompt_key}。可选型号：{', '.join(JSON_SCHEMA_MODELS)}"
        )
    llm = _build_llm(
        model,
        api_key,
        temperature,
        timeout=timeout,
        max_retries=max_retries,
        enable_thinking=enable_thinking,
    )
    return StructuredAgent(
        runnable=llm.bind(response_format=_json_schema_response_format(response_format)),
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
    # enable_milvus=False 时不初始化 RAG：本地检索由
    # tools.search_knowledge_base_records 降级为返回空结果并告警，
    # 图仍可跑通（本地知识库为空是合法场景）。此前该配置项无人读取，
    # 关掉它照样会连 Milvus 并在连不上时硬失败，整个服务起不来。
    if config.enable_milvus:
        init_rag_system(api_key=api_key, config=rag_config)
    else:
        logger.info("[models] enable_milvus=False，跳过 RAG 初始化；本地检索将返回空结果")

    # node_models 示例：
    # {"write": {"model": "qwen3.8-max"}, "intent_router": {"model": "qwen3.7-flash"}}
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

    def _structured_for(
        node_key: str,
        default_temp: float,
        response_format,
        enable_thinking: bool = False,
    ):
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
            enable_thinking=enable_thinking,
        )

    thinking_nodes = set(getattr(config, "thinking_nodes", None) or [])
    return AgentBundle(
        intent_router=_structured_for("intent_router", 0.0, IntentDecision),
        planner=_structured_for("plan", 0.3, PlanDraft),
        reflector=_structured_for("reflect", 0.3, ReflectionDraft),
        scout_web=_model_for("web_search", 0.4),
        scout_local=_model_for("local_rag", 0.4),
        evidence_judge=_model_for("deep_dive", 0.2, enable_thinking="deep_dive" in thinking_nodes),
        analyst=_structured_for(
            "analyze", 0.3, AnalysisDraft, enable_thinking="analyze" in thinking_nodes
        ),
        direct_responder=_model_for("direct_answer", 0.2),
        writer=_model_for("write", 0.4, enable_thinking="write" in thinking_nodes),
        # 澄清节点不再硬编码 qwen-turbo：它同样应可通过 node_models 分档
        clarifier=_model_for("clarify", 0.0),
    )
