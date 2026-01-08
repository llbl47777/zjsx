"""
LLM Scheduler Pro - 配置管理模块
Enterprise Edition v3.0

功能说明：
- 支持环境变量配置（前缀: LLM_）
- 支持配置热更新
- 支持配置验证
- 支持配置持久化

修复说明：
- 兼容 pydantic-settings 2.x 的环境变量解析
- 支持逗号分隔的字符串转换为列表
"""

import os
import json
import time
from typing import List, Dict, Optional, Any, Callable
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from threading import Lock
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 辅助函数：解析逗号分隔的字符串
# ============================================================
def parse_list_from_string(value: Any) -> List[str]:
    """
    将逗号分隔的字符串解析为列表

    参数:
        value: 输入值，可以是字符串或列表

    返回:
        List[str]: 解析后的字符串列表

    示例:
        parse_list_from_string("a,b,c") -> ["a", "b", "c"]
        parse_list_from_string(["a", "b"]) -> ["a", "b"]
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        # 处理逗号分隔的字符串
        return [item.strip() for item in value.split(',') if item.strip()]
    return []


def parse_int_list_from_string(value: Any) -> List[int]:
    """
    将逗号分隔的字符串解析为整数列表

    参数:
        value: 输入值，可以是字符串或列表

    返回:
        List[int]: 解析后的整数列表
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [int(x) for x in value]
    if isinstance(value, str):
        return [int(item.strip()) for item in value.split(',') if item.strip()]
    return []


def parse_float_list_from_string(value: Any) -> List[float]:
    """
    将逗号分隔的字符串解析为浮点数列表

    参数:
        value: 输入值，可以是字符串或列表

    返回:
        List[float]: 解析后的浮点数列表
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, str):
        return [float(item.strip()) for item in value.split(',') if item.strip()]
    return []


# ============================================================
# 配置类
# ============================================================
class Settings(BaseSettings):
    """
    全局配置类

    所有配置项都支持通过环境变量覆盖。
    环境变量前缀: LLM_
    例如: LLM_ROUTING_STRATEGY=p2c_kv

    使用示例:
        export LLM_WORKER_URLS="http://localhost:8000,http://localhost:8001"
        export LLM_ROUTING_STRATEGY="p2c_kv"
        export LLM_RATE_LIMIT_ENABLED="true"
    """

    # Pydantic Settings 配置（使用新的 SettingsConfigDict）
    model_config = SettingsConfigDict(
        env_prefix="LLM_",           # 环境变量前缀
        env_file=".env",             # 支持 .env 文件
        env_file_encoding="utf-8",   # 文件编码
        extra="ignore",              # 忽略额外的字段
        case_sensitive=False,        # 环境变量不区分大小写
    )

    # ============================================================
    # 基础配置 - 应用程序基本信息
    # ============================================================
    app_name: str = "LLM Scheduler Pro"      # 应用名称
    app_version: str = "3.0.0"               # 应用版本
    debug: bool = False                       # 调试模式开关
    log_level: str = "INFO"                   # 日志级别: DEBUG, INFO, WARNING, ERROR, CRITICAL

    # ============================================================
    # 服务器配置 - HTTP 服务器设置
    # ============================================================
    host: str = "0.0.0.0"                    # 监听地址
    port: int = 9000                         # 监听端口
    workers: int = 1                         # uvicorn 工作进程数

    # ============================================================
    # Worker 配置 - 后端 vLLM 服务器设置
    # ============================================================
    worker_urls: List[str] = Field(
        default=[
            "http://localhost:8000",
            "http://localhost:8001",
            "http://localhost:8002",
            "http://localhost:8003",
        ],
        description="后端 vLLM Worker 的 URL 列表"
    )
    worker_weights: Dict[str, float] = Field(
        default_factory=dict,
        description="Worker 权重映射 (URL -> weight)"
    )
    worker_tags: Dict[str, Dict[str, str]] = Field(
        default_factory=dict,
        description="Worker 标签映射 (URL -> tags)"
    )

    # ============================================================
    # 负载均衡配置 - 路由策略设置
    # ============================================================
    routing_strategy: str = "p2c_kv"         # 路由策略
    # 可选值:
    #   - rr: Round Robin 轮询
    #   - wrr: Weighted Round Robin 加权轮询
    #   - jsq: Join-Shortest-Queue 最短队列优先
    #   - lc: Least Connections 最少连接
    #   - p2c_kv: Power of Two Choices with KV-Cache awareness (推荐)
    #   - consistent_hash: 一致性哈希
    #   - adaptive: 自适应学习路由
    #   - random: 随机选择

    # P2C-KV 算法权重参数（用于计算节点负载得分）
    weight_waiting: float = 1.0              # 等待队列权重
    weight_running: float = 0.5              # 运行中请求权重
    weight_kv_cache: float = 2.0             # KV Cache 使用率权重
    weight_prompt: float = 0.1               # Prompt 长度权重
    weight_output: float = 0.1               # 预估输出长度权重
    weight_latency: float = 0.3              # 历史延迟权重
    weight_error_rate: float = 0.5           # 错误率权重

    # 一致性哈希配置
    hash_replicas: int = 150                 # 每个节点的虚拟节点数
    hash_algorithm: str = "md5"              # 哈希算法: md5, sha1, sha256

    # 自适应路由配置（基于强化学习）
    adaptive_window_size: int = 100          # 滑动窗口大小
    adaptive_learning_rate: float = 0.1      # 学习率
    adaptive_exploration_rate: float = 0.1   # 探索率 (epsilon-greedy)

    # ============================================================
    # 通信优化配置 - HTTP 连接池和超时设置
    # ============================================================
    metrics_interval: float = 0.5            # 指标采集间隔（秒）
    connection_pool_size: int = 100          # HTTP 连接池大小
    keepalive_connections: int = 20          # 保持活跃的连接数
    request_timeout: float = 300.0           # 总请求超时（秒）
    connect_timeout: float = 10.0            # 连接建立超时（秒）
    read_timeout: float = 300.0              # 读取响应超时（秒）
    write_timeout: float = 30.0              # 写入请求超时（秒）

    # 流式传输配置
    stream_chunk_size: int = 1024            # SSE 流式传输块大小（字节）
    stream_buffer_size: int = 65536          # 流式缓冲区大小（字节）

    # ============================================================
    # 健康检查/容错配置 - Worker 健康状态监控
    # ============================================================
    health_check_interval: float = 2.0       # 健康检查间隔（秒）
    health_check_timeout: float = 5.0        # 健康检查超时（秒）
    unhealthy_threshold: int = 2             # 连续失败次数 -> 标记为不健康
    recovery_threshold: int = 3              # 连续成功次数 -> 恢复为健康
    auto_recovery_enabled: bool = True       # 启用自动恢复检测
    warmup_requests: int = 5                 # 恢复后的预热请求数
    drain_timeout: float = 30.0              # 优雅下线超时（秒）

    # ============================================================
    # 重试配置 - 请求失败时的重试策略
    # ============================================================
    max_retries: int = 2                     # 最大重试次数
    retry_delay: float = 0.5                 # 初始重试延迟（秒）
    retry_backoff_multiplier: float = 2.0    # 重试延迟倍增系数
    retry_max_delay: float = 10.0            # 最大重试延迟（秒）
    retry_on_status_codes: List[int] = Field(
        default=[502, 503, 504],
        description="触发重试的 HTTP 状态码"
    )

    # ============================================================
    # 熔断器配置 - Circuit Breaker 保护机制
    # ============================================================
    circuit_breaker_enabled: bool = True     # 是否启用熔断器
    circuit_failure_threshold: int = 5       # 触发熔断的连续失败次数
    circuit_success_threshold: int = 3       # 恢复所需的连续成功次数
    circuit_timeout: float = 30.0            # 熔断超时时间（秒），之后进入半开状态
    circuit_half_open_requests: int = 3      # 半开状态允许的最大请求数
    circuit_failure_rate_threshold: float = 0.5    # 失败率阈值（0-1）
    circuit_slow_call_threshold_ms: float = 5000.0 # 慢调用判定阈值（毫秒）
    circuit_slow_call_rate_threshold: float = 0.8  # 慢调用率阈值（0-1）

    # ============================================================
    # 限流配置 - Rate Limiter 流量控制
    # ============================================================
    rate_limit_enabled: bool = True          # 是否启用限流
    rate_limit_requests_per_second: float = 100.0  # 全局 QPS 限制
    rate_limit_burst_size: int = 200         # 令牌桶突发容量
    rate_limit_per_user: bool = False        # 是否启用按用户限流
    rate_limit_user_rpm: int = 60            # 每用户每分钟请求数限制
    rate_limit_per_worker: bool = False      # 是否启用按 Worker 限流
    rate_limit_worker_qps: float = 50.0      # 每 Worker QPS 限制

    # ============================================================
    # 优先级队列配置 - 请求优先级管理
    # ============================================================
    priority_queue_enabled: bool = True      # 是否启用优先级队列
    priority_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "critical": 4.0,                 # 紧急优先级权重
            "high": 2.0,                     # 高优先级权重
            "normal": 1.0,                   # 正常优先级权重
            "low": 0.5,                      # 低优先级权重
        },
        description="优先级权重映射"
    )
    max_queue_size: int = 1000               # 最大队列长度
    queue_timeout: float = 60.0              # 队列等待超时（秒）

    # ============================================================
    # 指标配置 - Prometheus 监控指标
    # ============================================================
    metrics_enabled: bool = True             # 是否启用指标收集
    metrics_prefix: str = "llm_gateway"      # Prometheus 指标前缀
    metrics_retention_seconds: int = 3600    # 指标历史保留时间（秒）
    metrics_histogram_buckets: List[float] = Field(
        default=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        description="直方图桶边界"
    )

    # ============================================================
    # 追踪配置 - 分布式追踪 (OpenTelemetry)
    # ============================================================
    tracing_enabled: bool = True             # 是否启用追踪
    tracing_sample_rate: float = 1.0         # 采样率 (0-1)
    tracing_export_endpoint: str = ""        # OTLP 导出端点地址

    # ============================================================
    # 日志配置 - 请求/响应日志记录
    # ============================================================
    log_requests: bool = True                # 是否记录请求日志
    log_responses: bool = False              # 是否记录响应日志（注意隐私）
    log_format: str = "json"                 # 日志格式: json 或 text
    log_max_body_size: int = 1000            # 日志中 body 的最大长度

    # ============================================================
    # 告警配置 - 异常情况告警通知
    # ============================================================
    alerting_enabled: bool = True            # 是否启用告警
    alert_webhook_url: str = ""              # 告警 Webhook 推送地址
    alert_cooldown_seconds: float = 300.0    # 告警冷却时间（秒）
    alert_worker_down_threshold: int = 1     # Worker 下线告警阈值
    alert_error_rate_threshold: float = 0.1  # 错误率告警阈值 (0-1)
    alert_latency_threshold_ms: float = 5000.0  # 延迟告警阈值（毫秒）

    # ============================================================
    # Web UI 配置 - 内嵌 Dashboard 界面
    # ============================================================
    ui_enabled: bool = True                  # 是否启用 Web UI
    ui_path: str = "/ui"                     # Web UI 访问路径
    ui_auth_enabled: bool = False            # 是否启用 UI 认证
    ui_username: str = "admin"               # UI 登录用户名
    ui_password: str = "admin"               # UI 登录密码
    ui_session_timeout: int = 3600           # UI 会话超时（秒）

    # WebSocket 配置 - 实时数据推送
    ws_heartbeat_interval: float = 30.0      # WebSocket 心跳间隔（秒）
    ws_max_connections: int = 100            # 最大 WebSocket 连接数

    # ============================================================
    # 持久化配置 - 状态和配置的持久化存储
    # ============================================================
    data_dir: str = "./data"                 # 数据存储目录
    config_file: str = "./config.json"       # 配置文件路径
    state_persistence_enabled: bool = True   # 是否启用状态持久化
    state_save_interval: float = 60.0        # 状态保存间隔（秒）

    # ============================================================
    # 安全配置 - API 认证和 CORS
    # ============================================================
    api_key_enabled: bool = False            # 是否启用 API Key 认证
    api_keys: List[str] = Field(
        default_factory=list,
        description="允许的 API Key 列表"
    )
    cors_origins: List[str] = Field(
        default=["*"],
        description="允许的 CORS 来源"
    )
    cors_methods: List[str] = Field(
        default=["*"],
        description="允许的 CORS 方法"
    )
    cors_headers: List[str] = Field(
        default=["*"],
        description="允许的 CORS 头"
    )

    # ============================================================
    # 字段验证器 - 处理环境变量解析
    # ============================================================

    @field_validator('worker_urls', mode='before')
    @classmethod
    def parse_worker_urls(cls, v):
        """
        解析 worker_urls 字段

        支持格式:
            - 列表: ["http://a:8000", "http://b:8000"]
            - 逗号分隔字符串: "http://a:8000,http://b:8000"
        """
        return parse_list_from_string(v)

    @field_validator('api_keys', mode='before')
    @classmethod
    def parse_api_keys(cls, v):
        """解析 api_keys 字段"""
        return parse_list_from_string(v)

    @field_validator('cors_origins', mode='before')
    @classmethod
    def parse_cors_origins(cls, v):
        """解析 cors_origins 字段"""
        result = parse_list_from_string(v)
        return result if result else ["*"]

    @field_validator('cors_methods', mode='before')
    @classmethod
    def parse_cors_methods(cls, v):
        """解析 cors_methods 字段"""
        result = parse_list_from_string(v)
        return result if result else ["*"]

    @field_validator('cors_headers', mode='before')
    @classmethod
    def parse_cors_headers(cls, v):
        """解析 cors_headers 字段"""
        result = parse_list_from_string(v)
        return result if result else ["*"]

    @field_validator('retry_on_status_codes', mode='before')
    @classmethod
    def parse_retry_status_codes(cls, v):
        """解析 retry_on_status_codes 字段"""
        return parse_int_list_from_string(v)

    @field_validator('metrics_histogram_buckets', mode='before')
    @classmethod
    def parse_histogram_buckets(cls, v):
        """解析 metrics_histogram_buckets 字段"""
        return parse_float_list_from_string(v)

    @field_validator('routing_strategy')
    @classmethod
    def validate_routing_strategy(cls, v):
        """验证路由策略是否有效"""
        valid_strategies = [
            'rr',              # Round Robin
            'wrr',             # Weighted Round Robin
            'jsq',             # Join-Shortest-Queue
            'lc',              # Least Connections
            'p2c_kv',          # Power of Two Choices with KV awareness
            'consistent_hash', # Consistent Hashing
            'adaptive',        # Adaptive Learning
            'random'           # Random Selection
        ]
        if v not in valid_strategies:
            raise ValueError(
                f"routing_strategy 必须是以下之一: {valid_strategies}，"
                f"当前值: {v}"
            )
        return v

    @field_validator('log_level')
    @classmethod
    def validate_log_level(cls, v):
        """验证日志级别是否有效"""
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(
                f"log_level 必须是以下之一: {valid_levels}，"
                f"当前值: {v}"
            )
        return v_upper


# ============================================================
# 配置管理器类
# ============================================================
class ConfigManager:
    """
    配置管理器

    提供配置的动态管理功能：
    - 配置热更新（运行时修改配置）
    - 配置变更通知（观察者模式）
    - 配置历史记录
    - 配置持久化（保存/加载）
    - 配置验证

    使用示例:
        # 获取配置
        value = config_manager.get('routing_strategy')

        # 更新配置
        config_manager.set('routing_strategy', 'round_robin')

        # 添加配置变更监听器
        def on_config_change(key, old_val, new_val):
            print(f"{key}: {old_val} -> {new_val}")
        config_manager.add_listener(on_config_change)
    """

    def __init__(self, settings: Settings):
        """
        初始化配置管理器

        参数:
            settings: Settings 实例
        """
        self._settings = settings                    # 配置实例
        self._lock = Lock()                          # 线程锁
        self._listeners: List[Callable] = []         # 变更监听器列表
        self._history: List[Dict[str, Any]] = []     # 变更历史
        self._start_time = time.time()               # 启动时间

    @property
    def settings(self) -> Settings:
        """获取配置实例"""
        return self._settings

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项的值

        参数:
            key: 配置项名称
            default: 默认值（配置项不存在时返回）

        返回:
            配置项的值
        """
        return getattr(self._settings, key, default)

    def set(self, key: str, value: Any) -> bool:
        """
        设置配置项的值（支持热更新）

        参数:
            key: 配置项名称
            value: 新值

        返回:
            bool: 是否更新成功
        """
        with self._lock:
            # 检查配置项是否存在
            if not hasattr(self._settings, key):
                logger.warning(f"未知的配置项: {key}")
                return False

            # 获取旧值
            old_value = getattr(self._settings, key)
            if old_value == value:
                return True  # 值未变化，无需更新

            try:
                # 设置新值
                setattr(self._settings, key, value)

                # 记录变更历史
                self._history.append({
                    "timestamp": time.time(),
                    "key": key,
                    "old_value": old_value,
                    "new_value": value,
                })

                # 通知所有监听器
                for listener in self._listeners:
                    try:
                        listener(key, old_value, value)
                    except Exception as e:
                        logger.error(f"配置监听器执行失败: {e}")

                logger.info(f"配置已更新: {key} = {value}")
                return True

            except Exception as e:
                # 更新失败，回滚
                setattr(self._settings, key, old_value)
                logger.error(f"配置更新失败: {e}")
                return False

    def update(self, updates: Dict[str, Any]) -> Dict[str, bool]:
        """
        批量更新配置

        参数:
            updates: 配置更新字典 {key: value}

        返回:
            Dict[str, bool]: 每个配置项的更新结果
        """
        results = {}
        for key, value in updates.items():
            results[key] = self.set(key, value)
        return results

    def add_listener(self, listener: Callable[[str, Any, Any], None]):
        """
        添加配置变更监听器

        参数:
            listener: 回调函数，签名为 (key, old_value, new_value)
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str, Any, Any], None]):
        """
        移除配置变更监听器

        参数:
            listener: 要移除的回调函数
        """
        if listener in self._listeners:
            self._listeners.remove(listener)

    def get_snapshot(self) -> Dict[str, Any]:
        """
        获取当前配置快照

        返回:
            包含时间戳和所有配置项的字典
        """
        return {
            "timestamp": time.time(),
            "uptime_seconds": time.time() - self._start_time,
            "settings": {
                key: getattr(self._settings, key)
                for key in self._settings.model_fields.keys()
                if not key.startswith('_')
            }
        }

    def get_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取配置变更历史

        参数:
            limit: 返回的最大记录数

        返回:
            变更记录列表
        """
        return self._history[-limit:]

    def save_to_file(self, filepath: Optional[str] = None):
        """
        保存配置到文件

        参数:
            filepath: 文件路径（默认使用 settings.config_file）
        """
        filepath = filepath or self._settings.config_file
        snapshot = self.get_snapshot()

        # 确保目录存在
        dir_path = os.path.dirname(filepath)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(snapshot['settings'], f, indent=2, default=str, ensure_ascii=False)

        logger.info(f"配置已保存到: {filepath}")

    def load_from_file(self, filepath: Optional[str] = None) -> bool:
        """
        从文件加载配置

        参数:
            filepath: 文件路径（默认使用 settings.config_file）

        返回:
            bool: 是否加载成功
        """
        filepath = filepath or self._settings.config_file

        if not os.path.exists(filepath):
            logger.warning(f"配置文件不存在: {filepath}")
            return False

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            results = self.update(data)
            success_count = sum(1 for v in results.values() if v)
            logger.info(f"从 {filepath} 加载了 {success_count}/{len(data)} 个配置项")
            return True

        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return False

    def reset_to_defaults(self):
        """重置所有配置为默认值"""
        default_settings = Settings()
        for key in self._settings.model_fields.keys():
            if not key.startswith('_'):
                self.set(key, getattr(default_settings, key))
        logger.info("配置已重置为默认值")

    def validate(self) -> List[str]:
        """
        验证当前配置

        返回:
            List[str]: 错误信息列表（空列表表示配置有效）
        """
        errors = []

        # 检查 worker URLs
        if not self._settings.worker_urls:
            errors.append("未配置 Worker URLs")

        for url in self._settings.worker_urls:
            if not url.startswith(('http://', 'https://')):
                errors.append(f"无效的 Worker URL: {url}")

        # 检查权重值
        weights = [
            ('weight_waiting', self._settings.weight_waiting),
            ('weight_running', self._settings.weight_running),
            ('weight_kv_cache', self._settings.weight_kv_cache),
        ]
        for name, value in weights:
            if value < 0:
                errors.append(f"{name} 不能为负数")

        # 检查超时配置
        if self._settings.request_timeout <= 0:
            errors.append("request_timeout 必须为正数")

        if self._settings.health_check_timeout >= self._settings.health_check_interval:
            errors.append("health_check_timeout 应小于 health_check_interval")

        # 检查限流配置
        if self._settings.rate_limit_enabled:
            if self._settings.rate_limit_requests_per_second <= 0:
                errors.append("rate_limit_requests_per_second 必须为正数")
            if self._settings.rate_limit_burst_size < self._settings.rate_limit_requests_per_second:
                errors.append("rate_limit_burst_size 应大于等于 rate_limit_requests_per_second")

        return errors


# ============================================================
# 全局实例 - 应用程序使用这些全局变量
# ============================================================

# 创建配置实例
settings = Settings()

# 创建配置管理器实例
config_manager = ConfigManager(settings)


# ============================================================
# 配置辅助函数 - 提供便捷的配置访问方法
# ============================================================

def get_worker_weight(url: str) -> float:
    """
    获取指定 Worker 的权重

    参数:
        url: Worker URL

    返回:
        float: 权重值（默认 1.0）
    """
    return settings.worker_weights.get(url, 1.0)


def get_worker_tags(url: str) -> Dict[str, str]:
    """
    获取指定 Worker 的标签

    参数:
        url: Worker URL

    返回:
        Dict[str, str]: 标签字典
    """
    return settings.worker_tags.get(url, {})


def is_feature_enabled(feature: str) -> bool:
    """
    检查指定功能是否启用

    参数:
        feature: 功能名称

    返回:
        bool: 是否启用

    支持的功能:
        - rate_limit: 限流
        - circuit_breaker: 熔断器
        - priority_queue: 优先级队列
        - metrics: 指标收集
        - tracing: 分布式追踪
        - alerting: 告警
        - ui: Web UI
    """
    feature_map = {
        "rate_limit": settings.rate_limit_enabled,
        "circuit_breaker": settings.circuit_breaker_enabled,
        "priority_queue": settings.priority_queue_enabled,
        "metrics": settings.metrics_enabled,
        "tracing": settings.tracing_enabled,
        "alerting": settings.alerting_enabled,
        "ui": settings.ui_enabled,
    }
    return feature_map.get(feature, False)


def get_retry_delay(attempt: int) -> float:
    """
    计算重试延迟（指数退避算法）

    参数:
        attempt: 当前重试次数（从 0 开始）

    返回:
        float: 延迟时间（秒）

    公式:
        delay = min(base_delay * (multiplier ^ attempt), max_delay)
    """
    delay = settings.retry_delay * (settings.retry_backoff_multiplier ** attempt)
    return min(delay, settings.retry_max_delay)