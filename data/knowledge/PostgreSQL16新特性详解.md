# PostgreSQL 16 新特性详解

## 1. PostgreSQL 16 概述

PostgreSQL 16 于 2023 年 9 月 14 日正式发布，是 PostgreSQL 全球开发组发布的最新主要版本。本次版本在性能、安全、运维和扩展性方面均有显著提升。

## 2. 性能优化

### 2.1 并行查询增强

PostgreSQL 16 对并行查询做了多项优化：

- **并行顺序扫描**: 改进了对全表扫描的并行处理能力，支持更高的并行度
- **并行哈希连接**: 优化了哈希连接的并行执行计划
- **并行 LEFT JOIN**: 新增对左连接的并行支持
- **并行 FULL JOIN**: 新增对全连接的并行支持

### 2.2 聚合性能

- 改进了 `SUM()` 和 `AVG()` 在整数列上的聚合性能
- 增量排序 (Incremental Sort) 优化，减少排序开销
- 新增 `ANY_VALUE()` 聚合函数

### 2.3 COPY 性能

- `COPY` 命令在批量导入时性能提升约 300%
- 改进了 `COPY FROM` 在大文件场景下的内存管理

## 3. SQL 语法增强

### 3.1 JSON 路径表达式增强

```sql
-- SQL/JSON 路径查询增强
SELECT jsonb_path_query('[1,2,3,4,5]', '$[*] ? (@ > 2)');

-- 支持更复杂的 JSON 路径表达式
SELECT jsonb_path_query_tz(
  '{"events": [{"ts": "2023-01-01T10:00:00+08:00"}]}',
  '$.events[*].ts.datetime()'
);
```

### 3.2 新增统计函数

```sql
-- stats_timestamp 函数
SELECT stats_timestamp('pg_stat_user_tables');

-- 新增普通最小二乘法 (OLS) 回归函数
SELECT regr_slope(y, x) FROM my_table;
SELECT regr_intercept(y, x) FROM my_table;
```

### 3.3 EXPLAIN 增强

```sql
-- 显示 WAL (Write-Ahead Log) 统计信息
EXPLAIN (WAL, ANALYZE) SELECT * FROM large_table WHERE id = 100;

-- 支持序列化计划输出到 JSON 格式
EXPLAIN (FORMAT JSON, WAL) SELECT count(*) FROM orders;
```

## 4. 复制与高可用

### 4.1 逻辑复制增强

PostgreSQL 16 对逻辑复制做了重大改进：

- 支持从 standby 节点创建逻辑复制
- 改进大事务的复制性能
- 新增 `pg_logical_emit_message()` 函数
- 支持指定列的复制（Previously 只能整行复制）

```sql
-- 从 standby 创建逻辑复制槽
SELECT * FROM pg_create_logical_replication_slot('my_slot', 'pgoutput');

-- 发射逻辑消息
SELECT pg_logical_emit_message(true, 'my_app', 'transaction event');
```

### 4.2 同步复制增强

- 新增 `ANY` 同步模式（任一 standby 同步即可）
- 改进同步复制超时处理

## 5. 安全增强

### 5.1 SCRAM 认证

- 默认使用 SCRAM-SHA-256 认证
- 支持 SCRAM-SHA-256-PLUS（通道绑定）
- 改进密码轮换机制

### 5.2 客户端证书验证

```sql
-- 新增 pg_hba.conf 选项
-- clientcert=verify-full 选项强制验证客户端证书 CN
hostssl all all 0.0.0.0/0 cert clientcert=verify-full
```

### 5.3 行级安全 (RLS) 增强

- 改进 RLS 策略在 `UPDATE` 和 `DELETE` 上的性能
- 新增 `FORCE ROW LEVEL SECURITY` 选项

## 6. 监控与运维

### 6.1 新增系统视图

```sql
-- pg_stat_statements 增强
SELECT query, calls, total_exec_time, mean_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;

-- 新增 pg_stat_io 视图
SELECT backend_type, reads, writes
FROM pg_stat_io;

-- 新增 pg_stat_wal 视图
SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full
FROM pg_stat_wal;
```

### 6.2 自动化运维

- 改进 `autovacuum` 的调度策略
- 新增 `vacuumdb --buffer-usage-limit` 选项
- 改进大表的 VACUUM 性能

## 7. 扩展生态

### 7.1 新增扩展

- `pg_tle`: Trusted Language Extensions
- 改进 `pgvector` 扩展支持
- 增强 `pg_stat_statements` 统计维度

### 7.2 扩展管理

```sql
-- 改进扩展依赖管理
ALTER EXTENSION name UPDATE;
-- 支持扩展版本回退
ALTER EXTENSION name UPDATE TO '1.0';
```

## 8. 总结

PostgreSQL 16 是一个重要的里程碑版本，在并行查询、逻辑复制、安全认证、监控运维等方面都有显著提升。特别是从 standby 节点创建逻辑复制槽的能力，为高可用架构提供了更多灵活性。

WAL 统计信息的暴露有助于 DBA 更好地理解写入负载，从而优化配置参数。pg_stat_io 视图则提供了 I/O 层面的细粒度监控数据。
