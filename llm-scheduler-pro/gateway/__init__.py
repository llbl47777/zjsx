"""
LLM Scheduler Pro - Gateway Package
Enterprise Edition v3.0

A high-performance, intelligent LLM load balancing gateway.

Features:
- 7 Load Balancing Algorithms (RR, WRR, JSQ, LC, P2C-KV, Consistent Hash, Adaptive)
- Circuit Breaker Pattern
- Token Bucket Rate Limiting
- Auto Fault Recovery
- Real-time Metrics
- WebSocket Streaming
- OpenAI Compatible API
"""

from .app import app, main
from .config import settings, config_manager
from .router import router, Router, RoutingStrategy
from .metrics import metrics_collector, MetricsCollector
from .health import health_checker, HealthChecker
from .circuit_breaker import circuit_breaker_manager, CircuitBreaker, CircuitBreakerManager
from .rate_limiter import rate_limiter_manager, RateLimiter, RateLimiterManager
from .models import (
    WorkerState,
    WorkerStatus,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CircuitState,
    RateLimitResult,
)

__version__ = "3.0.0"
__author__ = "LLM Scheduler Team"
__all__ = [
    # App
    "app",
    "main",
    
    # Config
    "settings",
    "config_manager",
    
    # Router
    "router",
    "Router",
    "RoutingStrategy",
    
    # Metrics
    "metrics_collector",
    "MetricsCollector",
    
    # Health
    "health_checker",
    "HealthChecker",
    
    # Circuit Breaker
    "circuit_breaker_manager",
    "CircuitBreaker",
    "CircuitBreakerManager",
    
    # Rate Limiter
    "rate_limiter_manager",
    "RateLimiter",
    "RateLimiterManager",
    
    # Models
    "WorkerState",
    "WorkerStatus",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "CircuitState",
    "RateLimitResult",
]
