"""共享测试 fixture 与 mock 注册。

所有 P0-P8 测试文件共享此 conftest，统一处理第三方依赖 mock，
避免每个测试文件重复 _setup_mocks() 样板代码。

mock 注册策略：
- 对 langgraph / langchain_core / langchain_community 的子模块提供具体 mock 类
- 对 langmem / langchain_community 等其他包使用通配 mock finder
- test_p4.py / test_p5.py 保留各自的 _setup_mocks() 不受影响
  （因为 conftest 注册的 mock 与它们的 mock 内容一致）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/ -v
"""

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ── 通配 Meta Path Finder：自动 mock 未安装的第三方包 ──


class _MockLoader(importlib.abc.Loader):
    """创建空 mock 模块的 loader。"""

    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        pass  # 空实现


class _WildcardMockFinder(importlib.abc.MetaPathFinder):
    """对未安装的第三方包返回空 mock 模块，使 import 不报错。

    仅对白名单前缀生效，不影响 stdlib 和已安装的包。
    仅对不在 sys.modules 中的包生效（已注册的不覆盖）。
    若包已真实安装（标准 finder 能找到），则返回 None 让标准 finder 处理。
    """

    _PREFIXES = (
        "dashscope",
        "pymilvus",
        "faiss",
        "firecrawl",
        "searxng",
        "trustcall",
        "dydantic",
        # 基础设施包（测试环境可不安装）
        "minio",
        "pika",
        "redis",
        "sse_starlette",
        "starlette",
        "psycopg2",
        # langchain 扩展包（测试环境可不安装）
        "langchain_text_splitters",
        "langchain_experimental",
        "langchain_openai",
        "langchain_milvus",
    )

    def __init__(self):
        pass

    def _is_real_installed(self, name):
        """检查包是否真实安装（不触发 _WildcardMockFinder 自身）。

        直接使用 PathFinder（标准 sys.path 搜索器），绕过自定义 meta path finder。
        """
        try:
            spec = importlib.machinery.PathFinder.find_spec(name)
            return spec is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            return False

    def find_spec(self, name, path, target=None):
        for p in self._PREFIXES:
            if name == p or name.startswith(p + ".") or name.startswith(p + "_"):
                if name not in sys.modules:
                    if self._is_real_installed(name):
                        return None
                    return importlib.machinery.ModuleSpec(
                        name, _MockLoader(), is_package=True
                    )
        return None


def _register_langgraph_mocks():
    """注册 langgraph 子模块的具体 mock 类。"""
    if "langgraph" not in sys.modules:
        lg = types.ModuleType("langgraph")
        lg.__path__ = []
        sys.modules["langgraph"] = lg

    if "langgraph.types" not in sys.modules:
        lgt = types.ModuleType("langgraph.types")

        def _interrupt(value):
            return value

        class _Command:
            def __init__(self, goto=None, update=None, resume=None):
                self.goto = goto
                self.update = update or {}
                self.resume = resume

            def __repr__(self):
                return f"Command(goto={self.goto})"

        lgt.interrupt = _interrupt
        lgt.Command = _Command
        lgt.StreamWriter = type("StreamWriter", (), {})
        sys.modules["langgraph.types"] = lgt
        lg.types = lgt

    if "langgraph.graph" not in sys.modules:
        lgg = types.ModuleType("langgraph.graph")
        lgg.__path__ = []

        START = "__start__"
        END = "__end__"

        class _CompiledGraph:
            """Mock compiled graph: 支持 invoke / astream / get_graph / get_state 等。"""
            def __init__(self, nodes, edges):
                self._nodes = nodes
                self._edges = edges

            class _GraphView:
                def __init__(self, nodes):
                    self.nodes = {k: MagicMock() for k in nodes}
                    self.nodes["__start__"] = MagicMock()
                    self.nodes["__end__"] = MagicMock()

            def get_graph(self):
                return self._GraphView(self._nodes)

            def invoke(self, input_state, config=None):
                result = dict(input_state) if isinstance(input_state, dict) else {}
                result.setdefault("final", "mocked research report output")
                result.setdefault("intent", "multiagent")
                return result

            async def astream(self, input_state, config=None, stream_mode=None):
                yield ("updates", {k: MagicMock() for k in self._nodes})

            def get_state(self, config=None):
                snap = MagicMock()
                snap.values = {}
                snap.next = ()
                snap.interrupts = []
                snap.parent_config = None
                snap.tasks = ()
                snap.created_at = None
                snap.config = {"configurable": {}}
                return snap

            async def aget_state(self, config=None):
                return self.get_state(config)

            def update_state(self, config, values, as_node=None):
                pass

            def get_state_history(self, config=None):
                return iter([])

        class _StateGraph:
            def __init__(self, state_schema=None):
                self._nodes = {}
                self._edges = {}
                self._conditional = {}

            def add_node(self, name, fn):
                self._nodes[name] = fn

            def add_edge(self, src, dst):
                self._edges.setdefault(src, []).append(dst)

            def add_conditional_edges(self, src, fn, mapping=None):
                self._conditional[src] = (fn, mapping or {})

            def compile(self, checkpointer=None):
                return _CompiledGraph(self._nodes, self._edges)

        lgg.StateGraph = _StateGraph
        lgg.START = START
        lgg.END = END
        sys.modules["langgraph.graph"] = lgg
        lg.graph = lgg

    if "langgraph.graph.message" not in sys.modules:
        lgm = types.ModuleType("langgraph.graph.message")
        lgm.add_messages = lambda l, r: l + r
        sys.modules["langgraph.graph.message"] = lgm
        lgg.message = lgm

    if "langgraph.checkpoint.memory" not in sys.modules:
        lgcm = types.ModuleType("langgraph.checkpoint.memory")
        class _InMemorySaver:
            def __init__(self):
                pass
        lgcm.InMemorySaver = _InMemorySaver
        sys.modules["langgraph.checkpoint.memory"] = lgcm


def _register_langchain_core_mocks():
    """注册 langchain_core 子模块的具体 mock 类。"""
    if "langchain_core" not in sys.modules:
        lc = types.ModuleType("langchain_core")
        lc.__path__ = []
        sys.modules["langchain_core"] = lc

    if "langchain_core.messages" not in sys.modules:
        lcm = types.ModuleType("langchain_core.messages")

        class _HumanMessage:
            def __init__(self, content="", **kw):
                self.content = content
                self.type = "human"

        lcm.HumanMessage = _HumanMessage
        lcm.BaseMessage = type("BaseMessage", (), {})
        lcm.AIMessage = type("AIMessage", (), {})
        sys.modules["langchain_core.messages"] = lcm
        lc.messages = lcm

    if "langchain_core.tools" not in sys.modules:
        lct = types.ModuleType("langchain_core.tools")
        lct.tool = lambda f: f
        lct.StructuredTool = type("StructuredTool", (), {})
        sys.modules["langchain_core.tools"] = lct
        lc.tools = lct

    if "langchain_core.documents" not in sys.modules:
        lcd = types.ModuleType("langchain_core.documents")
        lcd.Document = type(
            "Document",
            (),
            {
                "__init__": lambda self, page_content="", metadata=None: (
                    setattr(self, "page_content", page_content),
                    setattr(self, "metadata", metadata or {}),
                )[-1]
            },
        )
        sys.modules["langchain_core.documents"] = lcd
        lc.documents = lcd


def _register_langchain_community_mocks():
    """注册 langchain_community 子模块的具体 mock 类。"""
    if "langchain_community" not in sys.modules:
        lcmm = types.ModuleType("langchain_community")
        lcmm.__path__ = []
        sys.modules["langchain_community"] = lcmm
    else:
        lcmm = sys.modules["langchain_community"]

    if "langchain_community.embeddings" not in sys.modules:
        lcmm_e = types.ModuleType("langchain_community.embeddings")
        lcmm_e.DashScopeEmbeddings = type(
            "DashScopeEmbeddings", (), {"__init__": lambda self, **kw: None}
        )
        sys.modules["langchain_community.embeddings"] = lcmm_e
        lcmm.embeddings = lcmm_e

    if "langchain_community.vectorstores" not in sys.modules:
        lcmm_v = types.ModuleType("langchain_community.vectorstores")
        lcmm_v.FAISS = type("FAISS", (), {})
        lcmm_v.Milvus = type("Milvus", (), {})
        sys.modules["langchain_community.vectorstores"] = lcmm_v
        lcmm.vectorstores = lcmm_v

    if "langchain_community.chat_models" not in sys.modules:
        lcmm_cm = types.ModuleType("langchain_community.chat_models")
        lcmm_cm.ChatTongyi = type(
            "ChatTongyi", (), {"__init__": lambda self, **kw: None}
        )
        sys.modules["langchain_community.chat_models"] = lcmm_cm
        lcmm.chat_models = lcmm_cm


def _register_typing_extensions():
    """确保 typing_extensions 可用。"""
    try:
        import typing_extensions  # noqa: F401
    except ImportError:
        te = types.ModuleType("typing_extensions")
        te.TypedDict = dict
        import typing

        te.Annotated = typing.Annotated
        sys.modules["typing_extensions"] = te


# ── 自动执行 mock 注册（仅注册到 sys.modules，不覆盖已有）──

try:
    import langgraph  # noqa: F401
except ImportError:
    _register_langgraph_mocks()

try:
    import langchain_core  # noqa: F401
except ImportError:
    _register_langchain_core_mocks()

try:
    import langchain_community  # noqa: F401
except ImportError:
    _register_langchain_community_mocks()

_register_typing_extensions()


def _register_langchain_mocks():
    """注册 langchain 顶层包及其 agents 子模块的具体 mock。"""
    if "langchain" not in sys.modules:
        lc = types.ModuleType("langchain")
        lc.__path__ = []
        sys.modules["langchain"] = lc
    else:
        lc = sys.modules["langchain"]

    if "langchain.agents" not in sys.modules:
        lc_ag = types.ModuleType("langchain.agents")
        lc_ag.create_agent = lambda **kw: MagicMock()
        sys.modules["langchain.agents"] = lc_ag
        lc.agents = lc_ag


try:
    import langchain  # noqa: F401
except ImportError:
    _register_langchain_mocks()

# 注册 fastapi mock（含 APIRouter 等具体类）
def _register_fastapi_mocks():
    if "fastapi" not in sys.modules:
        fa = types.ModuleType("fastapi")
        fa.__path__ = []
        class _APIRouter:
            def __init__(self, *args, **kwargs):
                self.routes = []
            def _add_route(self, path, methods, **kwargs):
                def decorator(func):
                    r = MagicMock()
                    r.path = path
                    r.methods = set(methods)
                    r.endpoint = func
                    self.routes.append(r)
                    return func
                return decorator
            def get(self, *args, **kwargs):
                return self._add_route(args[0] if args else kwargs.get('path', ''), ['GET'])
            def post(self, *args, **kwargs):
                return self._add_route(args[0] if args else kwargs.get('path', ''), ['POST'])
            def delete(self, *args, **kwargs):
                return self._add_route(args[0] if args else kwargs.get('path', ''), ['DELETE'])
            def patch(self, *args, **kwargs):
                return self._add_route(args[0] if args else kwargs.get('path', ''), ['PATCH'])
            def put(self, *args, **kwargs):
                return self._add_route(args[0] if args else kwargs.get('path', ''), ['PUT'])
        fa.APIRouter = _APIRouter
        fa.FastAPI = type("FastAPI", (), {"__init__": lambda self, *a, **kw: None})
        fa.Request = type("Request", (), {})
        fa.JSONResponse = type("JSONResponse", (), {"__init__": lambda self, *a, **kw: None})
        fa.CORSMiddleware = type("CORSMiddleware", (), {"__init__": lambda self, *a, **kw: None})
        fa.Depends = lambda *a, **kw: None
        fa.HTTPException = type("HTTPException", (Exception,), {"__init__": lambda self, *a, **kw: None})
        fa.UploadFile = type("UploadFile", (), {})
        fa.File = lambda *a, **kw: None
        fa.Form = lambda *a, **kw: None
        fa.Query = lambda *a, **kw: None
        fa.Body = lambda *a, **kw: None
        fa.Path = lambda *a, **kw: None
        sys.modules["fastapi"] = fa

    # 注册 fastapi.responses 子模块
    if "fastapi.responses" not in sys.modules:
        fa_resp = types.ModuleType("fastapi.responses")
        fa_resp.StreamingResponse = type("StreamingResponse", (), {"__init__": lambda self, *a, **kw: None})
        fa_resp.JSONResponse = type("JSONResponse", (), {"__init__": lambda self, *a, **kw: None})
        fa_resp.Response = type("Response", (), {"__init__": lambda self, *a, **kw: None})
        fa_resp.HTMLResponse = type("HTMLResponse", (), {"__init__": lambda self, *a, **kw: None})
        fa_resp.PlainTextResponse = type("PlainTextResponse", (), {"__init__": lambda self, *a, **kw: None})
        sys.modules["fastapi.responses"] = fa_resp
        fa.responses = fa_resp

    # 注册 fastapi.middleware.cors 子模块
    if "fastapi.middleware" not in sys.modules:
        fa_mid = types.ModuleType("fastapi.middleware")
        fa_mid.__path__ = []
        sys.modules["fastapi.middleware"] = fa_mid
        fa.middleware = fa_mid
    if "fastapi.middleware.cors" not in sys.modules:
        fa_cors = types.ModuleType("fastapi.middleware.cors")
        fa_cors.CORSMiddleware = type("CORSMiddleware", (), {"__init__": lambda self, *a, **kw: None})
        sys.modules["fastapi.middleware.cors"] = fa_cors
        fa_mid.cors = fa_cors

try:
    import fastapi  # noqa: F401
except ImportError:
    _register_fastapi_mocks()

# 注册通配 finder（最后挂载，优先级最低，不覆盖已注册的模块）
_finder = _WildcardMockFinder()
if not any(isinstance(f, _WildcardMockFinder) for f in sys.meta_path):
    sys.meta_path.append(_finder)


# ── 公共 fixture ──


@pytest.fixture
def project_root():
    """返回项目根路径。"""
    return _PROJECT_ROOT


@pytest.fixture
def app_path():
    """返回 app/ 路径。"""
    return _APP_PATH


@pytest.fixture
def make_initial_state():
    """创建测试用初始 AgentState 工厂。"""
    from mult_agents.state import create_initial_state

    def _factory(**overrides):
        defaults = dict(
            query="测试查询",
            max_iterations=3,
            user_id="u1",
            tenant_id="t1",
        )
        defaults.update(overrides)
        return create_initial_state(**defaults)

    return _factory


@pytest.fixture
def mock_writer():
    """StreamWriter mock 对象。"""
    return MagicMock()
