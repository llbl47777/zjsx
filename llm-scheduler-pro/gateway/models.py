"""
LLM Scheduler Pro - 数据模型
Enterprise Edition v3.0

包含：
- Worker状态模型
- 请求/响应模型
- 路由策略枚举
- 熔断器状态
- 限流器配置
- 指标数据结构
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
import time
import hashlib


# ============================================================
# 枚举定义
# ============================================================

class WorkerStatus(str, Enum):
    """Worker 状态枚举"""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"
    DRAINING = "draining"  # 优雅下线中
    WARMING_UP = "warming_up"  # 预热中


class RoutingStrategy(str, Enum):
    """路由策略枚举"""
    ROUND_ROBIN = "rr"
    WEIGHTED_ROUND_ROBIN = "wrr"
    LEAST_QUEUE = "jsq"
    LEAST_CONNECTIONS = "lc"
    P2C_KV_AWARE = "p2c_kv"
    CONSISTENT_HASH = "consistent_hash"
    ADAPTIVE = "adaptive"
    RANDOM = "random"


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常，允许请求通过
    OPEN = "open"          # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许部分请求探测


class AlertSeverity(str, Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RequestPriority(str, Enum):
    """请求优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================
# Worker 相关模型
# ============================================================

class WorkerState(BaseModel):
    """Worker 状态数据（增强版）"""
    url: str
    name: str = ""  # Worker名称
    status: WorkerStatus = WorkerStatus.HEALTHY
    weight: float = 1.0  # 权重（用于WRR）
    
    # 运行时指标
    running: int = 0              # 正在处理的请求数
    waiting: int = 0              # 等待队列中的请求数
    kv_cache_usage: float = 0.0   # KV Cache 使用率 (0-1)
    gpu_memory_usage: float = 0.0  # GPU显存使用率 (0-1)
    cpu_usage: float = 0.0         # CPU使用率 (0-1)
    
    # 健康检查状态
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_health_check: float = 0.0
    last_failure_reason: str = ""
    
    # 指标更新时间
    last_updated: float = 0.0
    
    # 统计信息
    total_requests: int = 0
    total_failures: int = 0
    total_success: int = 0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    
    # 延迟统计
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    min_latency_ms: float = float('inf')
    
    # 吞吐量
    requests_per_second: float = 0.0
    tokens_per_second: float = 0.0
    
    # 熔断器状态
    circuit_state: CircuitState = CircuitState.CLOSED
    circuit_opened_at: Optional[float] = None
    circuit_failure_count: int = 0
    
    # 元数据
    model_name: str = ""
    max_model_len: int = 0
    gpu_count: int = 1
    created_at: float = field(default_factory=time.time)
    
    class Config:
        use_enum_values = True


class WorkerConfig(BaseModel):
    """Worker 配置"""
    url: str
    name: str = ""
    weight: float = 1.0
    max_concurrent: int = 100
    warmup_requests: int = 5
    tags: Dict[str, str] = {}


# ============================================================
# 请求/响应模型
# ============================================================

class ChatMessage(BaseModel):
    """聊天消息"""
    role: str
    content: str
    name: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatCompletionRequest(BaseModel):
    """Chat Completion 请求（OpenAI兼容）"""
    model: str
    messages: List[ChatMessage]
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    repetition_penalty: Optional[float] = None
    stop: Optional[List[str]] = None
    n: Optional[int] = 1
    logprobs: Optional[bool] = None
    echo: Optional[bool] = None
    seed: Optional[int] = None
    user: Optional[str] = None
    
    # 扩展字段
    priority: RequestPriority = RequestPriority.NORMAL
    session_id: Optional[str] = None  # 用于一致性哈希
    timeout: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

    def get_hash_key(self) -> str:
        """获取一致性哈希的key"""
        if self.session_id:
            return self.session_id
        if self.user:
            return self.user
        # 使用消息内容的hash
        content = "".join(m.content for m in self.messages)
        return hashlib.md5(content.encode()).hexdigest()[:16]


class CompletionChoice(BaseModel):
    """完成选择"""
    index: int
    message: Optional[ChatMessage] = None
    delta: Optional[Dict[str, str]] = None
    finish_reason: Optional[str] = None
    logprobs: Optional[Any] = None


class CompletionUsage(BaseModel):
    """使用量统计"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """Chat Completion 响应"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[CompletionChoice]
    usage: Optional[CompletionUsage] = None
    system_fingerprint: Optional[str] = None


class ErrorResponse(BaseModel):
    """错误响应"""
    error: Dict[str, Any]
    request_id: Optional[str] = None


# ============================================================
# 请求上下文
# ============================================================

@dataclass
class RequestContext:
    """请求上下文（用于追踪和重试）"""
    request_id: str
    start_time: float = field(default_factory=time.time)
    retry_count: int = 0
    selected_worker: str = ""
    excluded_workers: List[str] = field(default_factory=list)
    
    # 性能指标
    ttft: float = 0.0  # Time to First Token
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    
    # 追踪信息
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    
    # 优先级
    priority: RequestPriority = RequestPriority.NORMAL
    
    def elapsed_ms(self) -> float:
        return (time.time() - self.start_time) * 1000
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "start_time": self.start_time,
            "retry_count": self.retry_count,
            "selected_worker": self.selected_worker,
            "ttft": self.ttft,
            "elapsed_ms": self.elapsed_ms(),
            "trace_id": self.trace_id,
        }


# ============================================================
# 熔断器模型
# ============================================================

@dataclass
class CircuitBreakerState:
    """熔断器状态"""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_state_change: float = field(default_factory=time.time)
    half_open_requests: int = 0
    
    # 配置
    failure_threshold: int = 5
    success_threshold: int = 3
    timeout_seconds: float = 30.0
    half_open_max_requests: int = 3


class CircuitBreakerConfig(BaseModel):
    """熔断器配置"""
    failure_threshold: int = 5  # 触发熔断的失败次数
    success_threshold: int = 3  # 恢复所需的成功次数
    timeout_seconds: float = 30.0  # 熔断超时时间
    half_open_max_requests: int = 3  # 半开状态最大请求数
    failure_rate_threshold: float = 0.5  # 失败率阈值
    slow_call_threshold_ms: float = 5000.0  # 慢调用阈值
    slow_call_rate_threshold: float = 0.8  # 慢调用率阈值


# ============================================================
# 限流器模型
# ============================================================

class RateLimitConfig(BaseModel):
    """限流配置"""
    enabled: bool = True
    requests_per_second: float = 100.0
    burst_size: int = 200
    per_worker_limit: bool = False
    per_user_limit: bool = False
    user_requests_per_minute: int = 60


@dataclass
class RateLimitState:
    """限流状态"""
    tokens: float = 0.0
    last_update: float = field(default_factory=time.time)
    total_allowed: int = 0
    total_rejected: int = 0


class RateLimitResult(BaseModel):
    """限流结果"""
    allowed: bool
    remaining: int
    retry_after: Optional[float] = None
    limit: int
    reset_at: float


# ============================================================
# 指标模型
# ============================================================

class MetricPoint(BaseModel):
    """单个指标数据点"""
    timestamp: float
    value: float
    labels: Dict[str, str] = {}


class LatencyHistogram(BaseModel):
    """延迟直方图"""
    count: int = 0
    sum: float = 0.0
    buckets: Dict[str, int] = {}  # bucket -> count
    
    # 预计算的分位数
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0


class WorkerMetrics(BaseModel):
    """Worker 详细指标"""
    url: str
    timestamp: float = field(default_factory=time.time)
    
    # 基础指标
    running_requests: int = 0
    waiting_requests: int = 0
    kv_cache_usage: float = 0.0
    
    # 吞吐量
    requests_total: int = 0
    requests_success: int = 0
    requests_failed: int = 0
    requests_per_second: float = 0.0
    
    # 延迟
    latency: LatencyHistogram = Field(default_factory=LatencyHistogram)
    ttft: LatencyHistogram = Field(default_factory=LatencyHistogram)
    
    # Token统计
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    tokens_per_second: float = 0.0
    
    # 熔断器状态
    circuit_state: CircuitState = CircuitState.CLOSED


class GatewayMetrics(BaseModel):
    """网关整体指标"""
    timestamp: float = field(default_factory=time.time)
    uptime_seconds: float = 0.0
    
    # 请求统计
    total_requests: int = 0
    active_requests: int = 0
    requests_per_second: float = 0.0
    
    # Worker状态
    healthy_workers: int = 0
    unhealthy_workers: int = 0
    total_workers: int = 0
    
    # 路由统计
    routing_strategy: str = ""
    route_distribution: Dict[str, int] = {}
    
    # 延迟
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    
    # 限流
    rate_limit_rejections: int = 0
    circuit_breaker_rejections: int = 0


# ============================================================
# 告警模型
# ============================================================

class Alert(BaseModel):
    """告警"""
    id: str
    severity: AlertSeverity
    title: str
    message: str
    source: str  # 告警来源（worker url 或 gateway）
    timestamp: float = Field(default_factory=time.time)
    resolved: bool = False
    resolved_at: Optional[float] = None
    labels: Dict[str, str] = {}
    annotations: Dict[str, str] = {}


class AlertRule(BaseModel):
    """告警规则"""
    name: str
    enabled: bool = True
    severity: AlertSeverity
    condition: str  # 条件表达式
    threshold: float
    duration_seconds: float = 0  # 持续时间
    message_template: str
    labels: Dict[str, str] = {}


# ============================================================
# 配置热更新模型
# ============================================================

class ConfigUpdate(BaseModel):
    """配置更新请求"""
    routing_strategy: Optional[str] = None
    weight_waiting: Optional[float] = None
    weight_running: Optional[float] = None
    weight_kv_cache: Optional[float] = None
    max_retries: Optional[int] = None
    retry_delay: Optional[float] = None
    rate_limit: Optional[RateLimitConfig] = None
    circuit_breaker: Optional[CircuitBreakerConfig] = None


class ConfigSnapshot(BaseModel):
    """配置快照"""
    timestamp: float = Field(default_factory=time.time)
    routing_strategy: str
    weights: Dict[str, float]
    rate_limit: RateLimitConfig
    circuit_breaker: CircuitBreakerConfig
    worker_configs: List[WorkerConfig]


# ============================================================
# WebSocket 消息模型
# ============================================================

class WSMessageType(str, Enum):
    """WebSocket 消息类型"""
    METRICS = "metrics"
    WORKER_STATUS = "worker_status"
    ALERT = "alert"
    CONFIG_UPDATE = "config_update"
    REQUEST_TRACE = "request_trace"
    HEARTBEAT = "heartbeat"


class WSMessage(BaseModel):
    """WebSocket 消息"""
    type: WSMessageType
    data: Dict[str, Any]
    timestamp: float = Field(default_factory=time.time)


# ============================================================
# 历史记录模型
# ============================================================

class RequestLog(BaseModel):
    """请求日志"""
    request_id: str
    timestamp: float
    worker_url: str
    method: str = "POST"
    path: str = "/v1/chat/completions"
    status_code: int
    latency_ms: float
    ttft_ms: Optional[float] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    retry_count: int = 0
    user: Optional[str] = None
    model: str = ""


class MetricsHistory(BaseModel):
    """指标历史"""
    worker_url: str
    metric_name: str
    points: List[MetricPoint] = []
    max_points: int = 3600  # 保留最近1小时（按秒）


# ============================================================
# API 响应模型
# ============================================================

class APIResponse(BaseModel):
    """通用 API 响应"""
    success: bool
    message: str = ""
    data: Optional[Any] = None
    error: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)


class PaginatedResponse(BaseModel):
    """分页响应"""
    items: List[Any]
    total: int
    page: int
    page_size: int
    total_pages: int
