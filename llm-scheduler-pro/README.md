# 🚀 LLM Scheduler Pro

**Enterprise Edition v3.0**

高性能分布式大语言模型负载均衡网关，为生产环境设计的智能路由解决方案。

---

## ✨ 特性

### 🎯 智能路由算法

| 算法 | 描述 | 适用场景 |
|------|------|----------|
| **Round Robin** | 轮询调度 | 均匀负载分布 |
| **Weighted Round Robin** | 加权轮询 | 异构服务器集群 |
| **Least Queue (JSQ)** | 最短队列优先 | 减少排队时间 |
| **Least Connections** | 最少连接 | 长连接场景 |
| **P2C-KV Aware** | 双随机选择 + KV缓存感知 | 🔥 推荐 - 智能负载均衡 |
| **Consistent Hash** | 一致性哈希 | 会话亲和性 |
| **Adaptive** | 自适应学习 | 动态负载环境 |

### 🛡️ 高可用保障

- **熔断器模式** - Netflix Hystrix 风格，自动故障隔离
- **自动重试** - 指数退避，智能重试策略
- **健康检查** - 主动探测 + 被动感知
- **优雅降级** - Draining 模式平滑下线

### 📊 实时监控

- **Web Dashboard** - 内置实时监控界面
- **WebSocket 推送** - 毫秒级状态更新
- **Prometheus 集成** - 完整指标导出
- **多维度统计** - QPS、延迟、吞吐量

### 🔧 企业级功能

- **令牌桶限流** - 多级限流保护
- **配置热更新** - 无需重启动态生效
- **OpenAI 兼容** - 无缝对接现有应用
- **流式传输** - SSE 实时响应

---

## 📦 快速开始

### 安装

```bash
# 克隆仓库
git clone https://github.com/your-org/llm-scheduler-pro.git
cd llm-scheduler-pro

# 安装依赖
pip install -r requirements.txt

# 启动网关
python -m uvicorn gateway.app:app --host 0.0.0.0 --port 9000
```

### Docker 部署

```bash
# 构建镜像
docker build -t llm-scheduler-pro .

# 启动服务
docker-compose up -d
```

### 环境变量配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `LLM_HOST` | `0.0.0.0` | 监听地址 |
| `LLM_PORT` | `9000` | 监听端口 |
| `LLM_WORKER_URLS` | - | Worker URL 列表（逗号分隔） |
| `LLM_ROUTING_STRATEGY` | `p2c_kv` | 路由策略 |
| `LLM_RATE_LIMIT_ENABLED` | `true` | 启用限流 |
| `LLM_CIRCUIT_BREAKER_ENABLED` | `true` | 启用熔断器 |

---

## 📡 API 接口

### OpenAI 兼容接口

```bash
# Chat Completions
curl -X POST http://localhost:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100
  }'

# 流式响应
curl -X POST http://localhost:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'

# 模型列表
curl http://localhost:9000/v1/models
```

### 管理接口

```bash
# 网关状态
curl http://localhost:9000/api/status

# Worker 列表
curl http://localhost:9000/api/workers

# 健康检查
curl http://localhost:9000/health

# Prometheus 指标
curl http://localhost:9000/metrics

# 路由统计
curl http://localhost:9000/api/routing/stats

# 熔断器状态
curl http://localhost:9000/api/circuit-breaker/stats

# 下线 Worker
curl -X POST http://localhost:9000/api/workers/0/drain

# 重置熔断器
curl -X POST http://localhost:9000/api/circuit-breaker/0/reset
```

---

## 🖥️ CLI 工具

```bash
# 查看状态
python scripts/cli.py status

# 列出 Workers
python scripts/cli.py workers

# 实时监控
python scripts/cli.py watch

# 下线 Worker
python scripts/cli.py drain 0

# 更新配置
python scripts/cli.py config --set routing_strategy=round_robin

# 健康检查
python scripts/cli.py health
```

---

## 📈 压力测试

```bash
# 基础测试
python scripts/load_test.py http://localhost:9000 -c 10 -d 30

# 高并发测试
python scripts/load_test.py http://localhost:9000 -c 100 -d 60

# 流式测试
python scripts/load_test.py http://localhost:9000 -c 20 -d 30 --stream

# 导出报告
python scripts/load_test.py http://localhost:9000 -c 50 -d 60 -o report.json
```

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                        Clients                               │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                  LLM Scheduler Pro Gateway                   │
│  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────────────┐ │
│  │  Rate   │ │ Circuit  │ │  Router   │ │    Metrics     │ │
│  │ Limiter │ │ Breaker  │ │ (7 algos) │ │   Collector    │ │
│  └─────────┘ └──────────┘ └───────────┘ └────────────────┘ │
│  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────────────┐ │
│  │ Health  │ │WebSocket │ │  Config   │ │   Dashboard    │ │
│  │ Checker │ │ Manager  │ │  Manager  │ │    (Web UI)    │ │
│  └─────────┘ └──────────┘ └───────────┘ └────────────────┘ │
└─────────────────────────┬───────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
   ┌────────────┐  ┌────────────┐  ┌────────────┐
   │   vLLM     │  │   vLLM     │  │   vLLM     │
   │  Worker 1  │  │  Worker 2  │  │  Worker N  │
   └────────────┘  └────────────┘  └────────────┘
```

---

## 📊 监控指标

### Prometheus 指标

```
# 网关指标
llm_gateway_requests_total
llm_gateway_active_requests
llm_gateway_request_latency_seconds
llm_gateway_healthy_workers

# Worker 指标
llm_worker_running_requests{url="..."}
llm_worker_waiting_requests{url="..."}
llm_worker_kv_cache_usage{url="..."}

# 熔断器指标
llm_circuit_breaker_state{url="..."}
llm_circuit_breaker_failures_total{url="..."}

# 限流指标
llm_rate_limiter_requests_total
llm_rate_limiter_rejected_total
```

---

## 🔧 配置详解

### 路由配置

```python
# P2C-KV 权重配置
p2c_weight_waiting = 1.0      # 等待队列权重
p2c_weight_running = 0.5      # 运行中权重
p2c_weight_kv_cache = 2.0     # KV缓存权重（推荐提高）
p2c_weight_latency = 0.3      # 延迟权重
p2c_weight_error_rate = 5.0   # 错误率权重

# 一致性哈希
consistent_hash_replicas = 150  # 虚拟节点数

# 自适应路由
adaptive_epsilon = 0.1         # 探索率
adaptive_learning_rate = 0.1   # 学习率
```

### 熔断器配置

```python
circuit_breaker_failure_threshold = 5      # 连续失败阈值
circuit_breaker_success_threshold = 3      # 恢复成功阈值
circuit_breaker_timeout_seconds = 30       # 熔断超时
circuit_breaker_half_open_requests = 3     # 半开状态最大请求
circuit_breaker_slow_call_threshold_ms = 5000  # 慢调用阈值
```

### 限流配置

```python
rate_limit_requests_per_second = 100  # 全局 QPS 限制
rate_limit_burst_size = 200           # 突发容量
rate_limit_per_user_rpm = 60          # 用户级别 RPM
rate_limit_per_worker_qps = 50        # Worker 级别 QPS
```

---

## 🧪 测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行特定测试
pytest tests/test_all.py::TestRouter -v

# 测试覆盖率
pytest tests/ --cov=gateway --cov-report=html

# 性能测试
pytest tests/test_all.py::TestPerformance -v -s
```

---

## 📁 项目结构

```
llm-scheduler-pro/
├── gateway/
│   ├── __init__.py        # 包初始化
│   ├── app.py             # FastAPI 主应用
│   ├── config.py          # 配置管理
│   ├── models.py          # 数据模型
│   ├── router.py          # 路由算法
│   ├── metrics.py         # 指标采集
│   ├── health.py          # 健康检查
│   ├── circuit_breaker.py # 熔断器
│   ├── rate_limiter.py    # 限流器
│   └── websocket.py       # WebSocket
├── scripts/
│   ├── cli.py             # CLI 工具
│   └── load_test.py       # 压力测试
├── tests/
│   └── test_all.py        # 测试套件
├── docker-compose.yml     # Docker 编排
├── Dockerfile             # 容器镜像
├── prometheus.yml         # Prometheus 配置
├── requirements.txt       # Python 依赖
└── README.md              # 项目文档
```

---

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

---

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE)

---

## 🙏 致谢

- [vLLM](https://github.com/vllm-project/vllm) - 高性能 LLM 推理引擎
- [FastAPI](https://fastapi.tiangolo.com/) - 现代 Python Web 框架
- [Netflix Hystrix](https://github.com/Netflix/Hystrix) - 熔断器模式灵感来源

---

**Made with ❤️ for the LLM Community**
