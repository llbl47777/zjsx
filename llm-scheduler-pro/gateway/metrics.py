"""
LLM Scheduler Pro - 指标采集模块
Enterprise Edition v3.0

功能：
- 采集 vLLM Worker 的 Prometheus 指标
- 支持 vLLM V0/V1 指标格式
- 指标历史存储
- Prometheus 格式导出
- 实时统计计算
"""

import asyncio
import time
import re
import logging
import threading
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass, field
import httpx
import statistics

from .models import (
    WorkerState, 
    WorkerStatus, 
    MetricPoint,
    LatencyHistogram,
    WorkerMetrics,
    GatewayMetrics,
)
from .config import settings

logger = logging.getLogger(__name__)


# ============================================================
# 延迟统计计算器
# ============================================================

class LatencyTracker:
    """
    延迟追踪器
    
    使用滑动窗口计算延迟分位数
    """
    
    def __init__(self, window_size: int = 1000):
        self.window_size = window_size
        self.latencies: deque = deque(maxlen=window_size)
        self._lock = threading.Lock()
        
        # 累计统计
        self.count = 0
        self.total_sum = 0.0
        self.min_val = float('inf')
        self.max_val = 0.0
    
    def record(self, latency_ms: float):
        """记录一个延迟值"""
        with self._lock:
            self.latencies.append(latency_ms)
            self.count += 1
            self.total_sum += latency_ms
            self.min_val = min(self.min_val, latency_ms)
            self.max_val = max(self.max_val, latency_ms)
    
    def get_percentile(self, p: float) -> float:
        """获取分位数"""
        with self._lock:
            if not self.latencies:
                return 0.0
            
            sorted_vals = sorted(self.latencies)
            idx = int(len(sorted_vals) * p / 100)
            idx = min(idx, len(sorted_vals) - 1)
            return sorted_vals[idx]
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            if not self.latencies:
                return {
                    "count": 0,
                    "avg": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "p50": 0.0,
                    "p95": 0.0,
                    "p99": 0.0,
                }
            
            sorted_vals = sorted(self.latencies)
            n = len(sorted_vals)
            
            return {
                "count": self.count,
                "avg": sum(self.latencies) / n,
                "min": self.min_val if self.min_val != float('inf') else 0.0,
                "max": self.max_val,
                "p50": sorted_vals[int(n * 0.5)],
                "p75": sorted_vals[int(n * 0.75)],
                "p90": sorted_vals[int(n * 0.90)],
                "p95": sorted_vals[int(n * 0.95)],
                "p99": sorted_vals[min(int(n * 0.99), n - 1)],
            }
    
    def get_histogram(self) -> LatencyHistogram:
        """获取直方图"""
        stats = self.get_stats()
        
        # 计算桶
        buckets = {}
        bucket_bounds = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
        
        with self._lock:
            for bound in bucket_bounds:
                buckets[str(bound)] = sum(1 for v in self.latencies if v <= bound)
        
        return LatencyHistogram(
            count=stats["count"],
            sum=self.total_sum,
            buckets=buckets,
            p50=stats["p50"],
            p75=stats["p75"],
            p90=stats["p90"],
            p95=stats["p95"],
            p99=stats["p99"],
        )
    
    def reset(self):
        """重置"""
        with self._lock:
            self.latencies.clear()
            self.count = 0
            self.total_sum = 0.0
            self.min_val = float('inf')
            self.max_val = 0.0


# ============================================================
# 吞吐量计算器
# ============================================================

class ThroughputTracker:
    """
    吞吐量追踪器
    
    计算 QPS 和 TPS (tokens per second)
    """
    
    def __init__(self, window_seconds: float = 60.0):
        self.window_seconds = window_seconds
        self.requests: deque = deque()  # (timestamp, tokens)
        self._lock = threading.Lock()
    
    def record(self, tokens: int = 0):
        """记录一次请求"""
        now = time.time()
        with self._lock:
            self.requests.append((now, tokens))
            self._cleanup()
    
    def _cleanup(self):
        """清理过期数据"""
        cutoff = time.time() - self.window_seconds
        while self.requests and self.requests[0][0] < cutoff:
            self.requests.popleft()
    
    def get_qps(self) -> float:
        """获取 QPS"""
        with self._lock:
            self._cleanup()
            if not self.requests:
                return 0.0
            
            elapsed = time.time() - self.requests[0][0]
            if elapsed <= 0:
                return 0.0
            
            return len(self.requests) / elapsed
    
    def get_tps(self) -> float:
        """获取 TPS (tokens per second)"""
        with self._lock:
            self._cleanup()
            if not self.requests:
                return 0.0
            
            elapsed = time.time() - self.requests[0][0]
            if elapsed <= 0:
                return 0.0
            
            total_tokens = sum(t for _, t in self.requests)
            return total_tokens / elapsed
    
    def get_stats(self) -> Dict:
        """获取统计"""
        return {
            "qps": self.get_qps(),
            "tps": self.get_tps(),
            "window_seconds": self.window_seconds,
            "samples": len(self.requests),
        }


# ============================================================
# 指标历史存储
# ============================================================

class MetricsHistory:
    """
    指标历史存储
    
    按时间序列存储指标数据
    """
    
    def __init__(self, max_points: int = 3600):
        self.max_points = max_points
        self.series: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_points))
        self._lock = threading.Lock()
    
    def add(self, metric_name: str, value: float, labels: Dict[str, str] = None):
        """添加数据点"""
        point = MetricPoint(
            timestamp=time.time(),
            value=value,
            labels=labels or {},
        )
        
        key = self._make_key(metric_name, labels)
        
        with self._lock:
            self.series[key].append(point)
    
    def _make_key(self, metric_name: str, labels: Dict[str, str] = None) -> str:
        """生成存储键"""
        if not labels:
            return metric_name
        
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{metric_name}{{{label_str}}}"
    
    def get(
        self, 
        metric_name: str, 
        labels: Dict[str, str] = None,
        start_time: float = None,
        end_time: float = None,
    ) -> List[MetricPoint]:
        """获取数据点"""
        key = self._make_key(metric_name, labels)
        
        with self._lock:
            points = list(self.series.get(key, []))
        
        # 时间过滤
        if start_time:
            points = [p for p in points if p.timestamp >= start_time]
        if end_time:
            points = [p for p in points if p.timestamp <= end_time]
        
        return points
    
    def get_latest(self, metric_name: str, labels: Dict[str, str] = None) -> Optional[MetricPoint]:
        """获取最新数据点"""
        points = self.get(metric_name, labels)
        return points[-1] if points else None
    
    def list_metrics(self) -> List[str]:
        """列出所有指标"""
        with self._lock:
            return list(self.series.keys())
    
    def clear(self):
        """清空历史"""
        with self._lock:
            self.series.clear()


# ============================================================
# 主指标采集器
# ============================================================

class MetricsCollector:
    """
    指标采集器
    
    功能：
    - 周期性采集 Worker 指标
    - 计算各类统计数据
    - 提供 Prometheus 格式导出
    """
    
    def __init__(self):
        # Worker 状态
        self.workers: Dict[str, WorkerState] = {}
        self._init_workers()
        
        # HTTP 客户端
        self.client: Optional[httpx.AsyncClient] = None
        
        # 后台任务
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # 延迟追踪
        self.latency_trackers: Dict[str, LatencyTracker] = defaultdict(LatencyTracker)
        self.ttft_trackers: Dict[str, LatencyTracker] = defaultdict(LatencyTracker)
        
        # 吞吐量追踪
        self.throughput_trackers: Dict[str, ThroughputTracker] = defaultdict(ThroughputTracker)
        
        # 指标历史
        self.history = MetricsHistory(max_points=settings.metrics_retention_seconds)
        
        # 网关级统计
        self.gateway_start_time = time.time()
        self.total_requests = 0
        self.active_requests = 0
        self._lock = threading.Lock()
        
        logger.info(f"MetricsCollector initialized with {len(self.workers)} workers")
    
    def _init_workers(self):
        """初始化 Worker 状态"""
        for i, url in enumerate(settings.worker_urls):
            self.workers[url] = WorkerState(
                url=url,
                name=f"worker-{i}",
                weight=settings.worker_weights.get(url, 1.0),
            )
    
    async def start(self):
        """启动采集"""
        self.client = httpx.AsyncClient(timeout=5.0)
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        logger.info(f"MetricsCollector started, interval={settings.metrics_interval}s")
    
    async def stop(self):
        """停止采集"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.aclose()
        logger.info("MetricsCollector stopped")
    
    async def _collection_loop(self):
        """采集循环"""
        while self._running:
            try:
                await self._collect_all_metrics()
            except Exception as e:
                logger.error(f"Metrics collection error: {e}")
            await asyncio.sleep(settings.metrics_interval)
    
    async def _collect_all_metrics(self):
        """并行采集所有 Worker 指标"""
        tasks = [self._collect_worker_metrics(url) for url in self.workers.keys()]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _collect_worker_metrics(self, url: str):
        """采集单个 Worker 指标"""
        worker = self.workers[url]
        
        # 跳过不健康的 Worker
        if worker.status == WorkerStatus.UNHEALTHY:
            return
        
        try:
            response = await self.client.get(f"{url}/metrics")
            if response.status_code == 200:
                metrics = self._parse_prometheus_metrics(response.text)
                
                # 更新 Worker 状态
                worker.running = int(metrics.get("num_requests_running", 0))
                worker.waiting = int(metrics.get("num_requests_waiting", 0))
                worker.kv_cache_usage = metrics.get("kv_cache_usage_perc", 0.0)
                worker.gpu_memory_usage = metrics.get("gpu_memory_usage", 0.0)
                worker.last_updated = time.time()
                
                # 记录历史
                labels = {"worker": url}
                self.history.add("running_requests", worker.running, labels)
                self.history.add("waiting_requests", worker.waiting, labels)
                self.history.add("kv_cache_usage", worker.kv_cache_usage, labels)
                
        except Exception as e:
            logger.debug(f"Failed to collect metrics from {url}: {e}")
    
    def _parse_prometheus_metrics(self, text: str) -> Dict[str, float]:
        """解析 Prometheus 格式指标"""
        metrics = {}
        
        patterns = {
            "num_requests_running": r"vllm:num_requests_running[^\n]*\s+(\d+(?:\.\d+)?)",
            "num_requests_waiting": r"vllm:num_requests_waiting[^\n]*\s+(\d+(?:\.\d+)?)",
            "kv_cache_usage_perc": r"vllm:(?:kv_cache_usage_perc|gpu_cache_usage_perc)[^\n]*\s+([\d.]+)",
            "gpu_memory_usage": r"vllm:gpu_memory_usage_bytes[^\n]*\s+(\d+(?:\.\d+)?)",
            "num_preemptions": r"vllm:num_preemptions_total[^\n]*\s+(\d+(?:\.\d+)?)",
            "avg_generation_throughput": r"vllm:avg_generation_throughput_toks_per_s[^\n]*\s+([\d.]+)",
            "avg_prompt_throughput": r"vllm:avg_prompt_throughput_toks_per_s[^\n]*\s+([\d.]+)",
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, text)
            if match:
                metrics[key] = float(match.group(1))
        
        return metrics
    
    # ============================================================
    # 请求统计接口
    # ============================================================
    
    def record_request_start(self):
        """记录请求开始"""
        with self._lock:
            self.total_requests += 1
            self.active_requests += 1
    
    def record_request_end(
        self, 
        worker_url: str, 
        latency_ms: float,
        ttft_ms: float = None,
        tokens: int = 0,
        success: bool = True,
    ):
        """记录请求结束"""
        with self._lock:
            self.active_requests = max(0, self.active_requests - 1)
        
        # 更新 Worker 统计
        if worker_url in self.workers:
            worker = self.workers[worker_url]
            worker.total_requests += 1
            if success:
                worker.total_success += 1
            else:
                worker.total_failures += 1
        
        # 记录延迟
        self.latency_trackers[worker_url].record(latency_ms)
        self.latency_trackers["_global"].record(latency_ms)
        
        if ttft_ms:
            self.ttft_trackers[worker_url].record(ttft_ms)
            self.ttft_trackers["_global"].record(ttft_ms)
        
        # 记录吞吐量
        self.throughput_trackers[worker_url].record(tokens)
        self.throughput_trackers["_global"].record(tokens)
        
        # 更新 Worker 延迟统计
        if worker_url in self.workers:
            worker = self.workers[worker_url]
            stats = self.latency_trackers[worker_url].get_stats()
            worker.avg_latency_ms = stats["avg"]
            worker.p50_latency_ms = stats["p50"]
            worker.p95_latency_ms = stats["p95"]
            worker.p99_latency_ms = stats["p99"]
            worker.min_latency_ms = stats["min"]
            worker.max_latency_ms = stats["max"]
            
            throughput = self.throughput_trackers[worker_url].get_stats()
            worker.requests_per_second = throughput["qps"]
            worker.tokens_per_second = throughput["tps"]
    
    def update_worker_stats(self, url: str, success: bool):
        """更新 Worker 请求统计"""
        if url in self.workers:
            worker = self.workers[url]
            worker.total_requests += 1
            if success:
                worker.total_success += 1
            else:
                worker.total_failures += 1
    
    # ============================================================
    # 查询接口
    # ============================================================
    
    def get_healthy_workers(self) -> Dict[str, WorkerState]:
        """获取健康的 Workers"""
        return {
            url: state for url, state in self.workers.items()
            if state.status == WorkerStatus.HEALTHY
        }
    
    def get_all_workers(self) -> Dict[str, WorkerState]:
        """获取所有 Workers"""
        return self.workers.copy()
    
    def get_worker(self, url: str) -> Optional[WorkerState]:
        """获取指定 Worker"""
        return self.workers.get(url)
    
    def get_worker_metrics(self, url: str) -> WorkerMetrics:
        """获取 Worker 详细指标"""
        worker = self.workers.get(url)
        if not worker:
            return None
        
        latency_stats = self.latency_trackers[url].get_stats()
        ttft_stats = self.ttft_trackers[url].get_stats()
        throughput_stats = self.throughput_trackers[url].get_stats()
        
        return WorkerMetrics(
            url=url,
            running_requests=worker.running,
            waiting_requests=worker.waiting,
            kv_cache_usage=worker.kv_cache_usage,
            requests_total=worker.total_requests,
            requests_success=worker.total_success,
            requests_failed=worker.total_failures,
            requests_per_second=throughput_stats["qps"],
            latency=self.latency_trackers[url].get_histogram(),
            ttft=self.ttft_trackers[url].get_histogram(),
            prompt_tokens_total=0,
            completion_tokens_total=0,
            tokens_per_second=throughput_stats["tps"],
            circuit_state=worker.circuit_state,
        )
    
    def get_gateway_metrics(self) -> GatewayMetrics:
        """获取网关整体指标"""
        with self._lock:
            healthy = sum(1 for w in self.workers.values() if w.status == WorkerStatus.HEALTHY)
            unhealthy = sum(1 for w in self.workers.values() if w.status == WorkerStatus.UNHEALTHY)
            
            global_latency = self.latency_trackers["_global"].get_stats()
            global_throughput = self.throughput_trackers["_global"].get_stats()
            
            return GatewayMetrics(
                uptime_seconds=time.time() - self.gateway_start_time,
                total_requests=self.total_requests,
                active_requests=self.active_requests,
                requests_per_second=global_throughput["qps"],
                healthy_workers=healthy,
                unhealthy_workers=unhealthy,
                total_workers=len(self.workers),
                routing_strategy=settings.routing_strategy,
                avg_latency_ms=global_latency["avg"],
                p95_latency_ms=global_latency["p95"],
                p99_latency_ms=global_latency["p99"],
            )
    
    # ============================================================
    # Prometheus 导出
    # ============================================================
    
    def export_prometheus(self) -> str:
        """导出 Prometheus 格式指标"""
        lines = []
        prefix = settings.metrics_prefix
        
        # 网关指标
        gateway = self.get_gateway_metrics()
        lines.append(f"# HELP {prefix}_uptime_seconds Gateway uptime in seconds")
        lines.append(f"# TYPE {prefix}_uptime_seconds gauge")
        lines.append(f"{prefix}_uptime_seconds {gateway.uptime_seconds:.2f}")
        
        lines.append(f"# HELP {prefix}_requests_total Total requests processed")
        lines.append(f"# TYPE {prefix}_requests_total counter")
        lines.append(f"{prefix}_requests_total {gateway.total_requests}")
        
        lines.append(f"# HELP {prefix}_active_requests Current active requests")
        lines.append(f"# TYPE {prefix}_active_requests gauge")
        lines.append(f"{prefix}_active_requests {gateway.active_requests}")
        
        lines.append(f"# HELP {prefix}_healthy_workers Number of healthy workers")
        lines.append(f"# TYPE {prefix}_healthy_workers gauge")
        lines.append(f"{prefix}_healthy_workers {gateway.healthy_workers}")
        
        # Worker 指标
        for url, worker in self.workers.items():
            labels = f'worker="{url}"'
            
            lines.append(f'{prefix}_worker_running{{{labels}}} {worker.running}')
            lines.append(f'{prefix}_worker_waiting{{{labels}}} {worker.waiting}')
            lines.append(f'{prefix}_worker_kv_cache_usage{{{labels}}} {worker.kv_cache_usage:.4f}')
            lines.append(f'{prefix}_worker_requests_total{{{labels}}} {worker.total_requests}')
            lines.append(f'{prefix}_worker_failures_total{{{labels}}} {worker.total_failures}')
            lines.append(f'{prefix}_worker_latency_avg_ms{{{labels}}} {worker.avg_latency_ms:.2f}')
            lines.append(f'{prefix}_worker_latency_p95_ms{{{labels}}} {worker.p95_latency_ms:.2f}')
            lines.append(f'{prefix}_worker_latency_p99_ms{{{labels}}} {worker.p99_latency_ms:.2f}')
        
        # 延迟直方图
        global_latency = self.latency_trackers["_global"].get_histogram()
        lines.append(f"# HELP {prefix}_request_latency_ms Request latency histogram")
        lines.append(f"# TYPE {prefix}_request_latency_ms histogram")
        
        for bucket, count in global_latency.buckets.items():
            lines.append(f'{prefix}_request_latency_ms_bucket{{le="{bucket}"}} {count}')
        
        lines.append(f'{prefix}_request_latency_ms_count {global_latency.count}')
        lines.append(f'{prefix}_request_latency_ms_sum {global_latency.sum:.2f}')
        
        return "\n".join(lines)
    
    def reset_stats(self):
        """重置统计"""
        for tracker in self.latency_trackers.values():
            tracker.reset()
        for tracker in self.ttft_trackers.values():
            tracker.reset()
        
        with self._lock:
            self.total_requests = 0
            self.active_requests = 0
        
        for worker in self.workers.values():
            worker.total_requests = 0
            worker.total_failures = 0
            worker.total_success = 0
        
        self.history.clear()
        logger.info("Metrics reset")


# ============================================================
# 全局单例
# ============================================================

metrics_collector = MetricsCollector()
