"""P2-2 实机验证脚本：真实 PG checkpointer 持久化闭环。

验证链：
1. init_checkpointer → AsyncPostgresSaver（非降级内存）
2. 真实 graph（真实 LLM）跑一次研究，checkpoint 落库
3. close_checkpointer 模拟「进程重启」，重新 init 后能从 PG 读到 checkpoint（持久化证明）
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, "app")

from mult_agents.runtime import init_checkpointer, close_checkpointer, get_checkpointer
from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.models import build_agents


async def main() -> None:
    cfg = AppConfig.from_file("config.json")
    thread_id = "p2_2_persist_verify"

    # 1. 初始化 checkpointer
    cp = await init_checkpointer(cfg)
    print("[1] checkpointer 类型:", type(cp).__name__)

    # 2. 构建真实 graph（真实 LLM agent）
    agents = build_agents(cfg.model, cfg.api_key, cfg)
    app = build_workflow_app(agents, cp)
    print("[2] graph 构建完成, checkpointer 绑定:", type(app.checkpointer).__name__ if hasattr(app, "checkpointer") else "N/A")

    # 3. 用真实 LLM 跑一次研究（direct 简单问题，走 direct_answer 节点）
    state = {
        "query": "什么是光合作用？请用一句话回答。",
        "max_iterations": 1,
        "user_id": "smoke_user",
        "tenant_id": "smoke_tenant",
        "memory_context": "",
        "hitl_enabled": False,
        "hitl_config": {},
    }
    config = {"configurable": {"thread_id": thread_id}}
    print("[3] 开始真实 LLM 研究（astream）...")
    final = ""
    async for mode, chunk in app.astream(state, config, stream_mode=["updates"]):
        if mode == "updates" and isinstance(chunk, dict):
            for node, out in chunk.items():
                if isinstance(out, dict) and out.get("final"):
                    final = str(out["final"])
    print("[3] 研究完成, final 长度:", len(final))
    print("     final 预览:", final[:80].replace("\n", " "))

    # 4. 验证 checkpoint 已落库（读 PG）
    snapshot = await app.aget_state(config)
    has_ckpt = snapshot.values.get("final", "") != "" or bool(snapshot.values)
    print("[4] checkpoint 落库, snapshot.values 键:", list(snapshot.values.keys())[:8])

    # 5. 模拟重启：关闭连接 → 重新初始化 → 读同一 thread 的 checkpoint
    await close_checkpointer()
    print("[5] 已关闭 checkpointer（模拟进程重启）")

    cp2 = await init_checkpointer(cfg)
    app2 = build_workflow_app(agents, cp2)
    snapshot2 = await app2.aget_state(config)
    final2 = str(snapshot2.values.get("final", ""))
    recovered = final2 != "" or bool(snapshot2.values)
    print("[6] 重启后从 PG 恢复 checkpoint:", "✅ 成功" if recovered else "❌ 失败")
    if recovered:
        print("     恢复的 final 长度:", len(final2))
        print("     恢复的 final 预览:", final2[:80].replace("\n", " "))

    await close_checkpointer()
    print("[7] 验证完成")


if __name__ == "__main__":
    asyncio.run(main())
