"""
LLM Scheduler Pro - 熔断器模块
Enterprise Edition v3.0

实现 Circuit Breaker 模式，防止级联故障

状态机：
    CLOSED (正常) 
        ↓ 失败次数达到阈值
    OPEN (熔断)
        ↓ 超时后
    HALF_OPEN (半开)
        ↓ 成功次数达到阈值 → CLOSED
        ↓ 失败 → OPEN

参考：
- Netflix Hystrix
- Resilience4j
"""

import time
import threading
import logging
from typing import Dict, Optional, Callable, Any, List
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
import asyncio

from .models import CircuitState, CircuitBreakerConfig
from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class CallResult:
    """调用结果"""
    success: bool
    latency_ms: float
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


class CircuitBreaker:
    """
    熔断器
    
    功能：
    - 故障检测与自动熔断
    - 超时后自动尝试恢复
    - 支持慢调用检测
    - 线程安全
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 3,
        timeout_seconds: float = 30.0,
        half_open_max_requests: int = 3,
        failure_rate_threshold: float = 0.5,
        slow_call_threshold_ms: float = 5000.0,
        slow_call_rate_threshold: float = 0.8,
        window_size: int = 100,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds
        self.half_open_max_requests = half_open_max_requests
        self.failure_rate_threshold = failure_rate_threshold
        self.slow_call_threshold_ms = slow_call_threshold_ms
        self.slow_call_rate_threshold = slow_call_rate_threshold
        self.window_size = window_size
        
        # 状态
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time = 0.0
        self._state_changed_at = time.time()
        
        # 滑动窗口（用于计算失败率）
        self._call_results: deque = deque(maxlen=window_size)
        
        # 统计
        self._total_calls = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejections = 0
        self._total_slow_calls = 0
        
        # 事件回调
        self._on_state_change: Optional[Callable[[CircuitState, CircuitState], None]] = None
        self._on_failure: Optional[Callable[[str], None]] = None
        
        # 锁
        self._lock = threading.RLock()
        
        logger.info(f"CircuitBreaker '{name}' created with threshold={failure_threshold}")
    
    @property
    def state(self) -> CircuitState:
        """获取当前状态"""
        with self._lock:
            self._check_state_timeout()
            return self._state
    
    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN
    
    @property
    def is_half_open(self) -> bool:
        return self.state == CircuitState.HALF_OPEN
    
    def _check_state_timeout(self):
        """检查是否需要状态转换（OPEN -> HALF_OPEN）"""
        if self._state == CircuitState.OPEN:
            if time.time() - self._state_changed_at >= self.timeout_seconds:
                self._transition_to(CircuitState.HALF_OPEN)
    
    def _transition_to(self, new_state: CircuitState):
        """状态转换"""
        old_state = self._state
        if old_state == new_state:
            return
        
        self._state = new_state
        self._state_changed_at = time.time()
        
        # 重置计数器
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._success_count = 0
        elif new_state == CircuitState.OPEN:
            self._half_open_calls = 0
        
        logger.warning(f"CircuitBreaker '{self.name}' state: {old_state.value} -> {new_state.value}")
        
        # 触发回调
        if self._on_state_change:
            try:
                self._on_state_change(old_state, new_state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")
    
    def allow_request(self) -> bool:
        """检查是否允许请求通过"""
        with self._lock:
            self._check_state_timeout()
            
            if self._state == CircuitState.CLOSED:
                return True
            
            elif self._state == CircuitState.OPEN:
                self._total_rejections += 1
                return False
            
            elif self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.half_open_max_requests:
                    self._half_open_calls += 1
                    return True
                else:
                    self._total_rejections += 1
                    return False
        
        return False
    
    def record_success(self, latency_ms: float):
        """记录成功调用"""
        with self._lock:
            self._total_calls += 1
            self._total_successes += 1
            
            # 检查慢调用
            is_slow = latency_ms > self.slow_call_threshold_ms
            if is_slow:
                self._total_slow_calls += 1
            
            # 记录到滑动窗口
            self._call_results.append(CallResult(
                success=not is_slow,  # 慢调用也算失败
                latency_ms=latency_ms
            ))
            
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            
            elif self._state == CircuitState.CLOSED:
                # 检查慢调用率
                if is_slow:
                    self._check_slow_call_rate()
    
    def record_failure(self, error: Optional[str] = None):
        """记录失败调用"""
        with self._lock:
            self._total_calls += 1
            self._total_failures += 1
            self._last_failure_time = time.time()
            
            # 记录到滑动窗口
            self._call_results.append(CallResult(
                success=False,
                latency_ms=0,
                error=error
            ))
            
            # 触发失败回调
            if self._on_failure:
                try:
                    self._on_failure(error or "Unknown error")
                except Exception as e:
                    logger.error(f"Failure callback error: {e}")
            
            if self._state == CircuitState.HALF_OPEN:
                # 半开状态下失败，直接熔断
                self._transition_to(CircuitState.OPEN)
            
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                
                # 检查是否达到阈值
                if self._failure_count >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                else:
                    # 检查失败率
                    self._check_failure_rate()
    
    def _check_failure_rate(self):
        """检查失败率是否超过阈值"""
        if len(self._call_results) < 10:  # 样本太少，不计算
            return
        
        failures = sum(1 for r in self._call_results if not r.success)
        failure_rate = failures / len(self._call_results)
        
        if failure_rate >= self.failure_rate_threshold:
            logger.warning(f"CircuitBreaker '{self.name}' failure rate {failure_rate:.2%} >= threshold")
            self._transition_to(CircuitState.OPEN)
    
    def _check_slow_call_rate(self):
        """检查慢调用率是否超过阈值"""
        if len(self._call_results) < 10:
            return
        
        slow_calls = sum(1 for r in self._call_results 
                        if r.success and r.latency_ms > self.slow_call_threshold_ms)
        slow_rate = slow_calls / len(self._call_results)
        
        if slow_rate >= self.slow_call_rate_threshold:
            logger.warning(f"CircuitBreaker '{self.name}' slow call rate {slow_rate:.2%} >= threshold")
            self._transition_to(CircuitState.OPEN)
    
    def reset(self):
        """手动重置熔断器"""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)
            self._call_results.clear()
            logger.info(f"CircuitBreaker '{self.name}' manually reset")
    
    def force_open(self):
        """手动强制熔断"""
        with self._lock:
            self._transition_to(CircuitState.OPEN)
            logger.info(f"CircuitBreaker '{self.name}' force opened")
    
    def on_state_change(self, callback: Callable[[CircuitState, CircuitState], None]):
        """设置状态变更回调"""
        self._on_state_change = callback
    
    def on_failure(self, callback: Callable[[str], None]):
        """设置失败回调"""
        self._on_failure = callback
    
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            failure_rate = 0.0
            if self._call_results:
                failures = sum(1 for r in self._call_results if not r.success)
                failure_rate = failures / len(self._call_results)
            
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "half_open_calls": self._half_open_calls,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "total_successes": self._total_successes,
                "total_rejections": self._total_rejections,
                "total_slow_calls": self._total_slow_calls,
                "failure_rate": failure_rate,
                "last_failure_time": self._last_failure_time,
                "state_changed_at": self._state_changed_at,
                "time_in_state": time.time() - self._state_changed_at,
            }


class CircuitBreakerManager:
    """
    熔断器管理器
    
    为每个 Worker 维护一个熔断器实例
    """
    
    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self._config = CircuitBreakerConfig(
            failure_threshold=settings.circuit_failure_threshold,
            success_threshold=settings.circuit_success_threshold,
            timeout_seconds=settings.circuit_timeout,
            half_open_max_requests=settings.circuit_half_open_requests,
            failure_rate_threshold=settings.circuit_failure_rate_threshold,
            slow_call_threshold_ms=settings.circuit_slow_call_threshold_ms,
            slow_call_rate_threshold=settings.circuit_slow_call_rate_threshold,
        )
        
        # 全局状态变更监听
        self._state_listeners: List[Callable] = []
    
    def get_breaker(self, worker_url: str) -> CircuitBreaker:
        """获取或创建熔断器"""
        with self._lock:
            if worker_url not in self._breakers:
                breaker = CircuitBreaker(
                    name=worker_url,
                    failure_threshold=self._config.failure_threshold,
                    success_threshold=self._config.success_threshold,
                    timeout_seconds=self._config.timeout_seconds,
                    half_open_max_requests=self._config.half_open_max_requests,
                    failure_rate_threshold=self._config.failure_rate_threshold,
                    slow_call_threshold_ms=self._config.slow_call_threshold_ms,
                    slow_call_rate_threshold=self._config.slow_call_rate_threshold,
                )
                
                # 设置状态变更监听
                breaker.on_state_change(
                    lambda old, new: self._notify_state_change(worker_url, old, new)
                )
                
                self._breakers[worker_url] = breaker
            
            return self._breakers[worker_url]
    
    def _notify_state_change(self, url: str, old: CircuitState, new: CircuitState):
        """通知状态变更"""
        for listener in self._state_listeners:
            try:
                listener(url, old, new)
            except Exception as e:
                logger.error(f"State listener error: {e}")
    
    def add_state_listener(self, listener: Callable):
        """添加状态变更监听器"""
        self._state_listeners.append(listener)
    
    def allow_request(self, worker_url: str) -> bool:
        """检查是否允许请求"""
        if not settings.circuit_breaker_enabled:
            return True
        return self.get_breaker(worker_url).allow_request()
    
    def record_success(self, worker_url: str, latency_ms: float):
        """记录成功"""
        if settings.circuit_breaker_enabled:
            self.get_breaker(worker_url).record_success(latency_ms)
    
    def record_failure(self, worker_url: str, error: Optional[str] = None):
        """记录失败"""
        if settings.circuit_breaker_enabled:
            self.get_breaker(worker_url).record_failure(error)
    
    def get_state(self, worker_url: str) -> CircuitState:
        """获取状态"""
        return self.get_breaker(worker_url).state
    
    def reset(self, worker_url: str):
        """重置指定熔断器"""
        self.get_breaker(worker_url).reset()
    
    def reset_all(self):
        """重置所有熔断器"""
        with self._lock:
            for breaker in self._breakers.values():
                breaker.reset()
    
    def force_open(self, worker_url: str):
        """强制熔断"""
        self.get_breaker(worker_url).force_open()
    
    def update_config(self, config: CircuitBreakerConfig):
        """更新配置"""
        self._config = config
        logger.info(f"CircuitBreaker config updated: {config}")
    
    def get_all_statistics(self) -> Dict[str, Dict]:
        """获取所有熔断器统计"""
        with self._lock:
            return {
                url: breaker.get_statistics()
                for url, breaker in self._breakers.items()
            }
    
    def get_open_breakers(self) -> List[str]:
        """获取所有处于 OPEN 状态的熔断器"""
        return [
            url for url, breaker in self._breakers.items()
            if breaker.is_open
        ]
    
    def get_summary(self) -> Dict:
        """获取汇总信息"""
        with self._lock:
            total = len(self._breakers)
            closed = sum(1 for b in self._breakers.values() if b.is_closed)
            open_count = sum(1 for b in self._breakers.values() if b.is_open)
            half_open = sum(1 for b in self._breakers.values() if b.is_half_open)
            
            return {
                "enabled": settings.circuit_breaker_enabled,
                "total": total,
                "closed": closed,
                "open": open_count,
                "half_open": half_open,
                "config": {
                    "failure_threshold": self._config.failure_threshold,
                    "timeout_seconds": self._config.timeout_seconds,
                    "failure_rate_threshold": self._config.failure_rate_threshold,
                }
            }


# ============================================================
# 熔断器装饰器
# ============================================================

def circuit_protected(breaker: CircuitBreaker):
    """
    熔断器保护装饰器（同步版本）
    
    用法:
        @circuit_protected(breaker)
        def my_function():
            ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            if not breaker.allow_request():
                raise CircuitOpenError(f"Circuit breaker '{breaker.name}' is open")
            
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                breaker.record_success(latency_ms)
                return result
            except Exception as e:
                breaker.record_failure(str(e))
                raise
        
        return wrapper
    return decorator


def async_circuit_protected(breaker: CircuitBreaker):
    """
    熔断器保护装饰器（异步版本）
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            if not breaker.allow_request():
                raise CircuitOpenError(f"Circuit breaker '{breaker.name}' is open")
            
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                breaker.record_success(latency_ms)
                return result
            except Exception as e:
                breaker.record_failure(str(e))
                raise
        
        return wrapper
    return decorator


class CircuitOpenError(Exception):
    """熔断器打开异常"""
    pass


# ============================================================
# 全局单例
# ============================================================

circuit_breaker_manager = CircuitBreakerManager()
