"""临时探针: 验证 P5-1 init_store() 能否在新 pgvector 容器上完成 setup()。

验证目标: docker-compose 选型 B 落地后, langmem 的 PostgresStore 阻塞点是否解除。
用法: <llmdev python> scripts/_probe_p5_store.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 app_main.py:13-22 一致: 必须在任何异步库被导入之前设置,
# 否则 psycopg3 异步模式在 Windows 上会 PoolTimeout
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(ROOT / "app"))

import asyncio  # noqa: E402  (必须在策略设置之后导入)


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


async def main() -> int:
    env = load_env()
    dsn = env.get("POSTGRES_DSN", "")
    api_key = env.get("DASHSCOPE_API_KEY", "")

    safe_dsn = dsn.split("@")[-1] if "@" in dsn else dsn
    print(f"[probe] target = {safe_dsn}")
    print(f"[probe] api_key present = {bool(api_key)}")

    from backend.infra.store_client import close_store, init_store

    try:
        store = await init_store(postgres_dsn=dsn, dashscope_api_key=api_key)
        print("[probe] SETUP_OK: init_store() 成功")
    except Exception as exc:
        print(f"[probe] SETUP_FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        pass

    # 确认 store 表族已建出来
    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' ORDER BY tablename"
                )
                tables = [r[0] for r in cur.fetchall()]
                print(f"[probe] tables = {tables}")
                cur.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='public' ORDER BY indexname"
                )
                idx = [r[0] for r in cur.fetchall()]
                print(f"[probe] indexes = {idx}")
    except Exception as exc:
        print(f"[probe] INSPECT_FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            await close_store()
        except Exception:
            pass

    print("[probe] RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
