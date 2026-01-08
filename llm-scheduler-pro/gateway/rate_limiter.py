"""
LLM Scheduler Pro - 限流器模块
Enterprise Edition v3.0

实现多种限流算法：
1. Token Bucket (令牌桶) - 默认
2. Sliding Window (滑动窗口)
3. Fixed Window (固定窗口)
4. Leaky Bucket (漏桶)

支持：
- 全局限流
- 每用户限流
- 每Worker限流
- 多级限流
"""

import time
import threading
import asyncio
import logging
from typing import Dict, Optional, Tuple, List, Callable
from dataclasses import dataclass, field
from collections import defaultdict, deque
from enum import Enum
import hashlib

from .models import RateLimitConfig, RateLimitResult
from .config import settings

logger = logging.getLogger(__name__)


class RateLimitAlgorithm(str, Enum):
    """限流算法"""
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW = "fixed_window"
    LEAKY_BUCKET = "leaky_bucket"


# ============================================================
# 令牌桶算法
# ============================================================

class TokenBucket:
    """
    令牌桶限流器
    
    特点：
    - 支持突发流量
    - 平滑限流
    """
    
    def __init__(
        self,
        rate: float,  # 每秒生成的令牌数
        capacity: int,  # 桶容量（最大令牌数）
    ):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)  # 当前令牌数
        self.last_update = time.time()
        self._lock = threading.Lock()
        
        # 统计
        self.total_allowed = 0
        self.total_rejected = 0
    
    def _refill(self):
        """补充令牌"""
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now
    
    def acquire(self, tokens: int = 1) -> Tuple[bool, float]:
        """
        尝试获取令牌
        
        Returns:
            (allowed, wait_time) - 是否允许，需要等待的时间
        """
        with self._lock:
            self._refill()
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                self.total_allowed += 1
                return True, 0.0
            else:
                self.total_rejected += 1
                # 计算需要等待的时间
                wait_time = (tokens - self.tokens) / self.rate
                return False, wait_time
    
    def try_acquire(self, tokens: int = 1) -> bool:
        """尝试获取令牌（不等待）"""
        allowed, _ = self.acquire(tokens)
        return allowed
    
    async def acquire_async(self, tokens: int = 1, max_wait: float = 0.0) -> bool:
        """异步获取令牌（可等待）"""
        allowed, wait_time = self.acquire(tokens)
        
        if allowed:
            return True
        
        if max_wait > 0 and wait_time <= max_wait:
            await asyncio.sleep(wait_time)
            return self.try_acquire(tokens)
        
        return False
    
    def get_stats(self) -> Dict:
        return {
            "rate": self.rate,
            "capacity": self.capacity,
            "current_tokens": self.tokens,
            "total_allowed": self.total_allowed,
            "total_rejected": self.total_rejected,
        }


# ============================================================
# 滑动窗口算法
# ============================================================

class SlidingWindowCounter:
    """
    滑动窗口限流器
    
    使用多个小窗口模拟滑动效果
    """
    
    def __init__(
        self,
        limit: int,  # 窗口内最大请求数
        window_size: float = 60.0,  # 窗口大小（秒）
        precision: int = 10,  # 精度（子窗口数）
    ):
        self.limit = limit
        self.window_size = window_size
        self.precision = precision
        self.slot_size = window_size / precision
        
        # 使用 deque 存储每个子窗口的计数
        self.slots: deque = deque(maxlen=precision)
        self.slot_timestamps: deque = deque(maxlen=precision)
        self.current_slot_count = 0
        self.current_slot_time = time.time()
        
        self._lock = threading.Lock()
        self.total_allowed = 0
        self.total_rejected = 0
    
    def _get_current_slot(self) -> int:
        """获取当前时间槽索引"""
        return int(time.time() / self.slot_size) % self.precision
    
    def _cleanup_old_slots(self):
        """清理过期的时间槽"""
        now = time.time()
        cutoff = now - self.window_size
        
        while self.slot_timestamps and self.slot_timestamps[0] < cutoff:
            self.slot_timestamps.popleft()
            self.slots.popleft()
    
    def _rotate_slot(self):
        """切换到新的时间槽"""
        now = time.time()
        
        if now - self.current_slot_time >= self.slot_size:
            # 保存当前槽
            self.slots.append(self.current_slot_count)
            self.slot_timestamps.append(self.current_slot_time)
            
            # 开始新槽
            self.current_slot_count = 0
            self.current_slot_time = now
            
            # 清理旧槽
            self._cleanup_old_slots()
    
    def acquire(self) -> Tuple[bool, int]:
        """
        尝试获取许可
        
        Returns:
            (allowed, remaining) - 是否允许，剩余配额
        """
        with self._lock:
            self._rotate_slot()
            
            # 计算当前窗口的总请求数
            total = sum(self.slots) + self.current_slot_count
            remaining = self.limit - total
            
            if total < self.limit:
                self.current_slot_count += 1
                self.total_allowed += 1
                return True, remaining - 1
            else:
                self.total_rejected += 1
                return False, 0
    
    def get_stats(self) -> Dict:
        with self._lock:
            total = sum(self.slots) + self.current_slot_count
            return {
                "limit": self.limit,
                "window_size": self.window_size,
                "current_count": total,
                "remaining": max(0, self.limit - total),
                "total_allowed": self.total_allowed,
                "total_rejected": self.total_rejected,
            }


# ============================================================
# 漏桶算法
# ============================================================

class LeakyBucket:
    """
    漏桶限流器
    
    特点：
    - 固定速率输出
    - 平滑流量
    - 不支持突发
    """
    
    def __init__(
        self,
        rate: float,  # 漏出速率（每秒）
        capacity: int,  # 桶容量
    ):
        self.rate = rate
        self.capacity = capacity
        self.water = 0.0  # 当前水量
        self.last_leak = time.time()
        self._lock = threading.Lock()
        
        self.total_allowed = 0
        self.total_rejected = 0
    
    def _leak(self):
        """漏水"""
        now = time.time()
        elapsed = now - self.last_leak
        leaked = elapsed * self.rate
        self.water = max(0, self.water - leaked)
        self.last_leak = now
    
    def acquire(self) -> Tuple[bool, float]:
        """
        尝试加水（请求）
        
        Returns:
            (allowed, queue_time) - 是否允许，预计排队时间
        """
        with self._lock:
            self._leak()
            
            if self.water < self.capacity:
                self.water += 1
                self.total_allowed += 1
                queue_time = self.water / self.rate
                return True, queue_time
            else:
                self.total_rejected += 1
                return False, 0.0
    
    def get_stats(self) -> Dict:
        with self._lock:
            self._leak()
            return {
                "rate": self.rate,
                "capacity": self.capacity,
                "current_water": self.water,
                "queue_time": self.water / self.rate,
                "total_allowed": self.total_allowed,
                "total_rejected": self.total_rejected,
            }


# ============================================================
# 统一限流器接口
# ============================================================

class RateLimiter:
    """
    统一限流器
    
    支持多种算法和多级限流
    """
    
    def __init__(
        self,
        requests_per_second: float = 100.0,
        burst_size: int = 200,
        algorithm: RateLimitAlgorithm = RateLimitAlgorithm.TOKEN_BUCKET,
    ):
        self.algorithm = algorithm
        
        # 创建限流器实例
        if algorithm == RateLimitAlgorithm.TOKEN_BUCKET:
            self._limiter = TokenBucket(requests_per_second, burst_size)
        elif algorithm == RateLimitAlgorithm.SLIDING_WINDOW:
            # 转换为每分钟限制
            self._limiter = SlidingWindowCounter(int(requests_per_second * 60), 60.0)
        elif algorithm == RateLimitAlgorithm.LEAKY_BUCKET:
            self._limiter = LeakyBucket(requests_per_second, burst_size)
        else:  # FIXED_WINDOW
            self._limiter = SlidingWindowCounter(int(requests_per_second * 60), 60.0, precision=1)
        
        # 用户级限流器
        self._user_limiters: Dict[str, TokenBucket] = {}
        self._user_lock = threading.Lock()
        
        # Worker级限流器
        self._worker_limiters: Dict[str, TokenBucket] = {}
        self._worker_lock = threading.Lock()
        
        # 配置
        self.user_rpm = settings.rate_limit_user_rpm
        self.worker_qps = settings.rate_limit_worker_qps
        
        logger.info(f"RateLimiter initialized: {algorithm.value}, "
                   f"rps={requests_per_second}, burst={burst_size}")
    
    def _get_user_limiter(self, user_id: str) -> TokenBucket:
        """获取用户限流器"""
        with self._user_lock:
            if user_id not in self._user_limiters:
                # 每用户每分钟限制，转换为每秒
                rps = self.user_rpm / 60.0
                self._user_limiters[user_id] = TokenBucket(rps, self.user_rpm // 2)
            return self._user_limiters[user_id]
    
    def _get_worker_limiter(self, worker_url: str) -> TokenBucket:
        """获取Worker限流器"""
        with self._worker_lock:
            if worker_url not in self._worker_limiters:
                self._worker_limiters[worker_url] = TokenBucket(
                    self.worker_qps, 
                    int(self.worker_qps * 2)
                )
            return self._worker_limiters[worker_url]
    
    def check(
        self,
        user_id: Optional[str] = None,
        worker_url: Optional[str] = None,
    ) -> RateLimitResult:
        """
        检查是否允许请求
        
        Args:
            user_id: 用户ID（可选）
            worker_url: Worker URL（可选）
        
        Returns:
            RateLimitResult
        """
        # 全局限流
        if isinstance(self._limiter, TokenBucket):
            allowed, wait_time = self._limiter.acquire()
            remaining = int(self._limiter.tokens)
            limit = self._limiter.capacity
        else:
            allowed, remaining = self._limiter.acquire()
            wait_time = 0.0 if allowed else 1.0
            limit = self._limiter.limit
        
        if not allowed:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after=wait_time,
                limit=limit,
                reset_at=time.time() + wait_time,
            )
        
        # 用户级限流
        if settings.rate_limit_per_user and user_id:
            user_limiter = self._get_user_limiter(user_id)
            user_allowed, user_wait = user_limiter.acquire()
            
            if not user_allowed:
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after=user_wait,
                    limit=self.user_rpm,
                    reset_at=time.time() + user_wait,
                )
        
        # Worker级限流
        if settings.rate_limit_per_worker and worker_url:
            worker_limiter = self._get_worker_limiter(worker_url)
            worker_allowed, worker_wait = worker_limiter.acquire()
            
            if not worker_allowed:
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after=worker_wait,
                    limit=int(self.worker_qps),
                    reset_at=time.time() + worker_wait,
                )
        
        return RateLimitResult(
            allowed=True,
            remaining=remaining,
            retry_after=None,
            limit=limit,
            reset_at=time.time() + 1.0,
        )
    
    async def check_async(
        self,
        user_id: Optional[str] = None,
        worker_url: Optional[str] = None,
        max_wait: float = 0.0,
    ) -> RateLimitResult:
        """异步检查（支持等待）"""
        result = self.check(user_id, worker_url)
        
        if not result.allowed and max_wait > 0 and result.retry_after:
            if result.retry_after <= max_wait:
                await asyncio.sleep(result.retry_after)
                return self.check(user_id, worker_url)
        
        return result
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = {
            "algorithm": self.algorithm.value,
            "global": self._limiter.get_stats(),
            "user_limiters": len(self._user_limiters),
            "worker_limiters": len(self._worker_limiters),
        }
        
        # 添加用户统计
        if self._user_limiters:
            stats["top_users"] = sorted(
                [
                    {"user": uid, "requests": lim.total_allowed + lim.total_rejected}
                    for uid, lim in self._user_limiters.items()
                ],
                key=lambda x: x["requests"],
                reverse=True
            )[:10]
        
        return stats
    
    def reset(self):
        """重置所有限流器"""
        if isinstance(self._limiter, TokenBucket):
            self._limiter.tokens = self._limiter.capacity
        
        with self._user_lock:
            self._user_limiters.clear()
        
        with self._worker_lock:
            self._worker_limiters.clear()
        
        logger.info("RateLimiter reset")
    
    def update_config(
        self,
        requests_per_second: Optional[float] = None,
        burst_size: Optional[int] = None,
        user_rpm: Optional[int] = None,
        worker_qps: Optional[float] = None,
    ):
        """更新配置"""
        if requests_per_second is not None or burst_size is not None:
            rps = requests_per_second or settings.rate_limit_requests_per_second
            burst = burst_size or settings.rate_limit_burst_size
            
            if isinstance(self._limiter, TokenBucket):
                self._limiter.rate = rps
                self._limiter.capacity = burst
        
        if user_rpm is not None:
            self.user_rpm = user_rpm
            with self._user_lock:
                self._user_limiters.clear()
        
        if worker_qps is not None:
            self.worker_qps = worker_qps
            with self._worker_lock:
                self._worker_limiters.clear()
        
        logger.info(f"RateLimiter config updated")


# ============================================================
# 限流器管理器
# ============================================================

class RateLimiterManager:
    """
    限流器管理器
    
    提供全局访问和配置管理
    """
    
    def __init__(self):
        self._limiter: Optional[RateLimiter] = None
        self._enabled = settings.rate_limit_enabled
        self._init_limiter()
    
    def _init_limiter(self):
        """初始化限流器"""
        self._limiter = RateLimiter(
            requests_per_second=settings.rate_limit_requests_per_second,
            burst_size=settings.rate_limit_burst_size,
        )
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        logger.info(f"RateLimiter {'enabled' if value else 'disabled'}")
    
    def check(
        self,
        user_id: Optional[str] = None,
        worker_url: Optional[str] = None,
    ) -> RateLimitResult:
        """检查限流"""
        if not self._enabled:
            return RateLimitResult(
                allowed=True,
                remaining=999999,
                limit=999999,
                reset_at=0,
            )
        
        return self._limiter.check(user_id, worker_url)
    
    async def check_async(
        self,
        user_id: Optional[str] = None,
        worker_url: Optional[str] = None,
        max_wait: float = 0.0,
    ) -> RateLimitResult:
        """异步检查"""
        if not self._enabled:
            return RateLimitResult(
                allowed=True,
                remaining=999999,
                limit=999999,
                reset_at=0,
            )
        
        return await self._limiter.check_async(user_id, worker_url, max_wait)
    
    def get_stats(self) -> Dict:
        """获取统计"""
        return {
            "enabled": self._enabled,
            **self._limiter.get_stats(),
        }
    
    def reset(self):
        """重置"""
        self._limiter.reset()
    
    def update_config(self, config: RateLimitConfig):
        """更新配置"""
        self._enabled = config.enabled
        self._limiter.update_config(
            requests_per_second=config.requests_per_second,
            burst_size=config.burst_size,
            user_rpm=config.user_requests_per_minute,
        )


# ============================================================
# 限流中间件
# ============================================================

class RateLimitMiddleware:
    """
    FastAPI 限流中间件
    """
    
    def __init__(self, manager: RateLimiterManager):
        self.manager = manager
    
    async def __call__(self, request, call_next):
        # 提取用户ID
        user_id = None
        if hasattr(request, 'headers'):
            user_id = request.headers.get('X-User-ID') or request.headers.get('Authorization')
        
        # 检查限流
        result = await self.manager.check_async(user_id=user_id)
        
        if not result.allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Rate limit exceeded",
                        "type": "rate_limit_error",
                        "retry_after": result.retry_after,
                    }
                },
                headers={
                    "Retry-After": str(int(result.retry_after or 1)),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": str(result.remaining),
                    "X-RateLimit-Reset": str(int(result.reset_at)),
                }
            )
        
        response = await call_next(request)
        
        # 添加限流头
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(int(result.reset_at))
        
        return response


# ============================================================
# 装饰器
# ============================================================

def rate_limited(
    limiter: RateLimiter,
    key_func: Optional[Callable] = None,
):
    """
    限流装饰器
    
    用法:
        @rate_limited(limiter)
        def my_function():
            ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            user_id = key_func(*args, **kwargs) if key_func else None
            result = limiter.check(user_id=user_id)
            
            if not result.allowed:
                raise RateLimitExceeded(
                    f"Rate limit exceeded, retry after {result.retry_after:.1f}s"
                )
            
            return func(*args, **kwargs)
        
        return wrapper
    return decorator


class RateLimitExceeded(Exception):
    """限流异常"""
    
    def __init__(self, message: str, retry_after: float = 1.0):
        super().__init__(message)
        self.retry_after = retry_after


# ============================================================
# 全局单例
# ============================================================

rate_limiter_manager = RateLimiterManager()
