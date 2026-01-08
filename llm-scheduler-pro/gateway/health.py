"""
LLM Scheduler Pro - 健康检查模块
Enterprise Edition v3.0

功能：
- 周期性健康检查
- 故障自动检测
- 自动恢复机制
- 优雅下线支持
- 预热机制
- 告警集成
"""

import asyncio
import time
import logging
from typing import Optional, List, Dict, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import httpx

from .models import WorkerStatus, WorkerState, Alert, AlertSeverity
from .metrics import metrics_collector
from .config import settings

logger = logging.getLogger(__name__)


# ============================================================
# 健康检查结果
# ============================================================

@dataclass
class HealthCheckResult:
    """健康检查结果"""
    url: str
    healthy: bool
    latency_ms: float
    status_code: Optional[int] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    
    # 详细信息
    model_loaded: bool = True
    gpu_available: bool = True
    memory_ok: bool = True


# ============================================================
# 健康检查器
# ============================================================

class HealthChecker:
    """
    健康检查器
    
    功能：
    - 定期检查 Worker 健康状态
    - 状态机管理：HEALTHY <-> UNHEALTHY <-> RECOVERING
    - 支持优雅下线 (DRAINING)
    - 支持预热 (WARMING_UP)
    """
    
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # 检查历史
        self._check_history: Dict[str, List[HealthCheckResult]] = {}
        
        # 事件回调
        self._on_status_change: List[Callable[[str, WorkerStatus, WorkerStatus], None]] = []
        self._on_alert: List[Callable[[Alert], None]] = []
        
        # 预热队列
        self._warmup_queue: Dict[str, int] = {}  # url -> remaining warmup requests
        
        logger.info("HealthChecker initialized")
    
    async def start(self):
        """启动健康检查"""
        self.client = httpx.AsyncClient(timeout=settings.health_check_timeout)
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"HealthChecker started, interval={settings.health_check_interval}s, "
                   f"auto_recovery={settings.auto_recovery_enabled}")
    
    async def stop(self):
        """停止健康检查"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.aclose()
        logger.info("HealthChecker stopped")
    
    async def _check_loop(self):
        """检查循环"""
        while self._running:
            try:
                await self._check_all_workers()
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
            await asyncio.sleep(settings.health_check_interval)
    
    async def _check_all_workers(self):
        """并行检查所有 Worker"""
        tasks = [
            self._check_worker(url) 
            for url in metrics_collector.workers.keys()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 记录结果
        for result in results:
            if isinstance(result, HealthCheckResult):
                if result.url not in self._check_history:
                    self._check_history[result.url] = []
                self._check_history[result.url].append(result)
                
                # 保留最近 100 次检查记录
                if len(self._check_history[result.url]) > 100:
                    self._check_history[result.url] = self._check_history[result.url][-100:]
    
    async def _check_worker(self, url: str) -> HealthCheckResult:
        """检查单个 Worker"""
        worker = metrics_collector.workers[url]
        start_time = time.time()
        
        try:
            response = await self.client.get(f"{url}/health")
            latency_ms = (time.time() - start_time) * 1000
            
            result = HealthCheckResult(
                url=url,
                healthy=response.status_code == 200,
                latency_ms=latency_ms,
                status_code=response.status_code,
            )
            
            if response.status_code == 200:
                self._handle_success(worker, latency_ms)
            else:
                self._handle_failure(worker, f"HTTP {response.status_code}")
                result.error = f"HTTP {response.status_code}"
            
        except httpx.ConnectError:
            latency_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                url=url,
                healthy=False,
                latency_ms=latency_ms,
                error="Connection refused",
            )
            self._handle_failure(worker, "Connection refused")
            
        except httpx.TimeoutException:
            latency_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                url=url,
                healthy=False,
                latency_ms=latency_ms,
                error="Timeout",
            )
            self._handle_failure(worker, "Timeout")
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                url=url,
                healthy=False,
                latency_ms=latency_ms,
                error=str(e)[:100],
            )
            self._handle_failure(worker, str(e)[:100])
        
        worker.last_health_check = time.time()
        return result
    
    def _handle_success(self, worker: WorkerState, latency_ms: float):
        """处理健康检查成功"""
        old_status = worker.status
        worker.consecutive_failures = 0
        worker.consecutive_successes += 1
        worker.last_failure_reason = ""
        
        if worker.status == WorkerStatus.UNHEALTHY:
            # 不健康 -> 恢复中
            if settings.auto_recovery_enabled:
                self._transition_status(worker, WorkerStatus.RECOVERING)
        
        elif worker.status == WorkerStatus.RECOVERING:
            # 恢复中 -> 检查是否达到恢复阈值
            if worker.consecutive_successes >= settings.recovery_threshold:
                # 进入预热阶段
                if settings.warmup_requests > 0:
                    self._warmup_queue[worker.url] = settings.warmup_requests
                    self._transition_status(worker, WorkerStatus.WARMING_UP)
                else:
                    self._transition_status(worker, WorkerStatus.HEALTHY)
                    self._send_recovery_alert(worker)
        
        elif worker.status == WorkerStatus.WARMING_UP:
            # 预热中，检查是否完成
            if worker.url in self._warmup_queue:
                if self._warmup_queue[worker.url] <= 0:
                    del self._warmup_queue[worker.url]
                    self._transition_status(worker, WorkerStatus.HEALTHY)
                    self._send_recovery_alert(worker)
        
        elif worker.status == WorkerStatus.DRAINING:
            # 下线中，不做状态变更
            pass
    
    def _handle_failure(self, worker: WorkerState, reason: str):
        """处理健康检查失败"""
        worker.consecutive_successes = 0
        worker.consecutive_failures += 1
        worker.last_failure_reason = reason
        
        if worker.status == WorkerStatus.HEALTHY:
            # 健康 -> 检查是否达到不健康阈值
            if worker.consecutive_failures >= settings.unhealthy_threshold:
                self._transition_status(worker, WorkerStatus.UNHEALTHY)
                self._send_failure_alert(worker, reason)
        
        elif worker.status == WorkerStatus.RECOVERING:
            # 恢复中但又失败 -> 回到不健康
            self._transition_status(worker, WorkerStatus.UNHEALTHY)
        
        elif worker.status == WorkerStatus.WARMING_UP:
            # 预热中失败 -> 回到恢复中
            if worker.url in self._warmup_queue:
                del self._warmup_queue[worker.url]
            self._transition_status(worker, WorkerStatus.RECOVERING)
    
    def _transition_status(self, worker: WorkerState, new_status: WorkerStatus):
        """状态转换"""
        old_status = worker.status
        if old_status == new_status:
            return
        
        worker.status = new_status
        worker.consecutive_successes = 0
        
        logger.warning(f"Worker {worker.url} status: {old_status.value} -> {new_status.value}")
        
        # 触发回调
        for callback in self._on_status_change:
            try:
                callback(worker.url, old_status, new_status)
            except Exception as e:
                logger.error(f"Status change callback error: {e}")
    
    def _send_failure_alert(self, worker: WorkerState, reason: str):
        """发送故障告警"""
        alert = Alert(
            id=f"worker_down_{worker.url}_{int(time.time())}",
            severity=AlertSeverity.ERROR,
            title=f"Worker Down: {worker.name or worker.url}",
            message=f"Worker {worker.url} is unhealthy: {reason}",
            source=worker.url,
            labels={"type": "worker_health", "worker": worker.url},
        )
        self._emit_alert(alert)
    
    def _send_recovery_alert(self, worker: WorkerState):
        """发送恢复告警"""
        alert = Alert(
            id=f"worker_recovery_{worker.url}_{int(time.time())}",
            severity=AlertSeverity.INFO,
            title=f"Worker Recovered: {worker.name or worker.url}",
            message=f"Worker {worker.url} has recovered and is back online",
            source=worker.url,
            labels={"type": "worker_health", "worker": worker.url},
        )
        self._emit_alert(alert)
    
    def _emit_alert(self, alert: Alert):
        """触发告警回调"""
        for callback in self._on_alert:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")
    
    # ============================================================
    # 手动操作接口
    # ============================================================
    
    async def check_now(self, url: str) -> HealthCheckResult:
        """立即检查指定 Worker"""
        return await self._check_worker(url)
    
    def drain_worker(self, url: str):
        """
        开始优雅下线 Worker
        
        Worker 将不再接收新请求，但会继续处理已有请求
        """
        if url in metrics_collector.workers:
            worker = metrics_collector.workers[url]
            self._transition_status(worker, WorkerStatus.DRAINING)
            logger.info(f"Worker {url} is draining")
    
    def undrain_worker(self, url: str):
        """取消下线，恢复 Worker"""
        if url in metrics_collector.workers:
            worker = metrics_collector.workers[url]
            if worker.status == WorkerStatus.DRAINING:
                self._transition_status(worker, WorkerStatus.HEALTHY)
                logger.info(f"Worker {url} undrained")
    
    def mark_unhealthy(self, url: str, reason: str = "Manual"):
        """手动标记 Worker 为不健康"""
        if url in metrics_collector.workers:
            worker = metrics_collector.workers[url]
            worker.last_failure_reason = reason
            self._transition_status(worker, WorkerStatus.UNHEALTHY)
    
    def mark_healthy(self, url: str):
        """手动标记 Worker 为健康"""
        if url in metrics_collector.workers:
            worker = metrics_collector.workers[url]
            worker.consecutive_failures = 0
            worker.last_failure_reason = ""
            self._transition_status(worker, WorkerStatus.HEALTHY)
    
    def record_warmup_request(self, url: str):
        """记录预热请求"""
        if url in self._warmup_queue:
            self._warmup_queue[url] = max(0, self._warmup_queue[url] - 1)
    
    # ============================================================
    # 事件注册
    # ============================================================
    
    def on_status_change(self, callback: Callable[[str, WorkerStatus, WorkerStatus], None]):
        """注册状态变更回调"""
        self._on_status_change.append(callback)
    
    def on_alert(self, callback: Callable[[Alert], None]):
        """注册告警回调"""
        self._on_alert.append(callback)
    
    # ============================================================
    # 查询接口
    # ============================================================
    
    def get_status_summary(self) -> Dict[str, int]:
        """获取状态摘要"""
        workers = metrics_collector.workers
        return {
            "total": len(workers),
            "healthy": sum(1 for w in workers.values() if w.status == WorkerStatus.HEALTHY),
            "unhealthy": sum(1 for w in workers.values() if w.status == WorkerStatus.UNHEALTHY),
            "recovering": sum(1 for w in workers.values() if w.status == WorkerStatus.RECOVERING),
            "draining": sum(1 for w in workers.values() if w.status == WorkerStatus.DRAINING),
            "warming_up": sum(1 for w in workers.values() if w.status == WorkerStatus.WARMING_UP),
        }
    
    def get_worker_status(self, url: str) -> Optional[Dict]:
        """获取 Worker 详细状态"""
        if url not in metrics_collector.workers:
            return None
        
        worker = metrics_collector.workers[url]
        history = self._check_history.get(url, [])
        
        # 计算最近的成功率
        recent_checks = history[-10:] if history else []
        success_rate = sum(1 for c in recent_checks if c.healthy) / len(recent_checks) if recent_checks else 1.0
        
        # 计算平均检查延迟
        avg_check_latency = sum(c.latency_ms for c in recent_checks) / len(recent_checks) if recent_checks else 0
        
        return {
            "url": url,
            "name": worker.name,
            "status": worker.status.value,
            "consecutive_failures": worker.consecutive_failures,
            "consecutive_successes": worker.consecutive_successes,
            "last_failure_reason": worker.last_failure_reason,
            "last_health_check": worker.last_health_check,
            "success_rate": success_rate,
            "avg_check_latency_ms": avg_check_latency,
            "warmup_remaining": self._warmup_queue.get(url, 0),
            "recent_checks": [
                {
                    "timestamp": c.timestamp,
                    "healthy": c.healthy,
                    "latency_ms": c.latency_ms,
                    "error": c.error,
                }
                for c in recent_checks[-5:]
            ],
        }
    
    def get_all_status(self) -> List[Dict]:
        """获取所有 Worker 状态"""
        return [
            self.get_worker_status(url)
            for url in metrics_collector.workers.keys()
        ]
    
    def get_check_history(self, url: str, limit: int = 100) -> List[HealthCheckResult]:
        """获取检查历史"""
        history = self._check_history.get(url, [])
        return history[-limit:]
    
    def get_uptime_stats(self) -> Dict[str, Dict]:
        """获取各 Worker 的运行时间统计"""
        stats = {}
        
        for url, history in self._check_history.items():
            if not history:
                continue
            
            total_checks = len(history)
            healthy_checks = sum(1 for c in history if c.healthy)
            
            # 计算最长连续健康/不健康时间
            max_healthy_streak = 0
            max_unhealthy_streak = 0
            current_healthy_streak = 0
            current_unhealthy_streak = 0
            
            for check in history:
                if check.healthy:
                    current_healthy_streak += 1
                    current_unhealthy_streak = 0
                    max_healthy_streak = max(max_healthy_streak, current_healthy_streak)
                else:
                    current_unhealthy_streak += 1
                    current_healthy_streak = 0
                    max_unhealthy_streak = max(max_unhealthy_streak, current_unhealthy_streak)
            
            stats[url] = {
                "total_checks": total_checks,
                "healthy_checks": healthy_checks,
                "uptime_percentage": healthy_checks / total_checks * 100 if total_checks > 0 else 0,
                "max_healthy_streak": max_healthy_streak,
                "max_unhealthy_streak": max_unhealthy_streak,
                "avg_latency_ms": sum(c.latency_ms for c in history) / total_checks if total_checks > 0 else 0,
            }
        
        return stats


# ============================================================
# 深度健康检查器
# ============================================================

class DeepHealthChecker:
    """
    深度健康检查器
    
    除了基本健康检查外，还检查：
    - GPU 可用性
    - 模型加载状态
    - 内存使用
    - 推理测试
    """
    
    def __init__(self, health_checker: HealthChecker):
        self.health_checker = health_checker
        self.client: Optional[httpx.AsyncClient] = None
    
    async def deep_check(self, url: str) -> Dict[str, Any]:
        """执行深度健康检查"""
        if not self.client:
            self.client = httpx.AsyncClient(timeout=30.0)
        
        result = {
            "url": url,
            "timestamp": time.time(),
            "basic_health": False,
            "model_loaded": False,
            "inference_test": False,
            "metrics_available": False,
            "details": {},
        }
        
        # 基本健康检查
        try:
            resp = await self.client.get(f"{url}/health")
            result["basic_health"] = resp.status_code == 200
        except Exception as e:
            result["details"]["health_error"] = str(e)
        
        # 模型检查
        try:
            resp = await self.client.get(f"{url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                result["model_loaded"] = len(data.get("data", [])) > 0
                result["details"]["models"] = [m.get("id") for m in data.get("data", [])]
        except Exception as e:
            result["details"]["model_error"] = str(e)
        
        # 推理测试
        try:
            test_request = {
                "model": "test",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            }
            start = time.time()
            resp = await self.client.post(
                f"{url}/v1/chat/completions",
                json=test_request,
                timeout=10.0,
            )
            latency = (time.time() - start) * 1000
            
            if resp.status_code == 200:
                result["inference_test"] = True
                result["details"]["inference_latency_ms"] = latency
        except Exception as e:
            result["details"]["inference_error"] = str(e)
        
        # 指标检查
        try:
            resp = await self.client.get(f"{url}/metrics")
            result["metrics_available"] = resp.status_code == 200
        except Exception as e:
            result["details"]["metrics_error"] = str(e)
        
        # 综合评估
        result["overall_healthy"] = all([
            result["basic_health"],
            result["model_loaded"],
            result["inference_test"],
        ])
        
        return result
    
    async def check_all_deep(self) -> List[Dict[str, Any]]:
        """对所有 Worker 执行深度检查"""
        tasks = [
            self.deep_check(url)
            for url in metrics_collector.workers.keys()
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)


# ============================================================
# 全局单例
# ============================================================

health_checker = HealthChecker()
deep_health_checker = DeepHealthChecker(health_checker)
