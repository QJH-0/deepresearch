-- postgres 服务(镜像 pgvector/pgvector:pg16) 初始化脚本
-- 挂载点: /docker-entrypoint-initdb.d/init_pgvector.sql (见 docker-compose.middleware.yml)
-- 执行时机: 仅在 PGDATA 首次初始化时由官方 entrypoint 自动执行一次, 幂等可重复

-- LangGraph PostgresStore (langmem 长期记忆底座) 的两个必需扩展:
--   vector  -> 语义检索: store.asearch(namespace, query=...) 走向量相似度
--   pg_trgm -> 关键词检索: PostgresStore 的文本匹配索引依赖 trigram
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 校验: 若扩展缺失, 容器初始化应在此处失败而不是等到运行时才报错
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector(vector) 扩展创建失败, PostgresStore 语义检索将不可用';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        RAISE EXCEPTION 'pg_trgm 扩展创建失败, PostgresStore 关键词检索将不可用';
    END IF;
END
$$;
