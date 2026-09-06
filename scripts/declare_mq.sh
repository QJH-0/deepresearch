#!/bin/bash
# RabbitMQ 队列拓扑声明脚本（在容器内执行）
# 用法: docker exec middleware-rabbitmq bash /tmp/declare_mq.sh

set -e

ADMIN_USER="admin"
ADMIN_PASS="admin123456"

# 声明死信交换机
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare exchange --name chunk-sync-dlx --type topic --durable true

# 声明死信队列（lazy 模式）
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare queue --name chunk-sync-dlq --durable true

# 绑定死信队列到死信交换机
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare binding --source chunk-sync-dlx --destination-type queue --destination chunk-sync-dlq --routing-key "chunk.sync.dead"

# 声明业务交换机
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare exchange --name chunk-sync --type topic --durable true

# 写参数 JSON 到文件（避免 shell 引号问题）
cat > /tmp/queue_args.json << 'ENDJSON'
{"x-dead-letter-exchange":"chunk-sync-dlx","x-dead-letter-routing-key":"chunk.sync.dead"}
ENDJSON

# 声明业务队列（带 DLX 参数）—— 用 rabbitmqadmin 的 arguments JSON
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare queue --name chunk-sync.queue --durable true --arguments "$(cat /tmp/queue_args.json)"

# 绑定业务队列到业务交换机
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" declare binding --source chunk-sync --destination-type queue --destination chunk-sync.queue --routing-key "chunk.sync.#"

echo ""
echo "=== RabbitMQ topology declared successfully ==="
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" list queues name messages arguments
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" list exchanges name type durable
rabbitmqadmin -u "$ADMIN_USER" -p "$ADMIN_PASS" list bindings source_name source_type destination_name destination_type routing_key
