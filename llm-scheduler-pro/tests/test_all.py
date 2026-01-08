"""
LLM Scheduler Pro - 测试套件
Enterprise Edition v3.0

包含：
- 单元测试
- 集成测试
- 性能测试
- 压力测试
"""

import pytest
import asyncio
import time
import random
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Dict, List
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# 测试配置
# ============================================================

@pytest.fixture
def mock_settings():
    """模拟配置"""
    from gateway.config import Settings
    return Settings(
        worker_urls=["http://worker1:8000", "http://worker2:8000", "http://worker3:8000"],
        routing_strategy="p2c_kv",
        rate_limit_enabled=False,
        circuit_breaker_enabled=False,
    )


@pytest.fixture
def mock_worker_states():
    """模拟Worker状态"""
    from gateway.models import WorkerState, WorkerStatus
    return {
        "http://worker1:8000": WorkerState(
            url="http://worker1:8000",
            name="worker-1",
            status=WorkerStatus.HEALTHY,
            running=5,
            waiting=2,
            kv_cache_usage=0.3,
        ),
        "http://worker2:8000": WorkerState(
            url="http://worker2:8000",
            name="worker-2",
            status=WorkerStatus.HEALTHY,
            running=10,
            waiting=5,
            kv_cache_usage=0.6,
        ),
        "http://worker3:8000": WorkerState(
            url="http://worker3:8000",
            name="worker-3",
            status=WorkerStatus.UNHEALTHY,
            running=0,
            waiting=0,
            kv_cache_usage=0.0,
        ),
    }


@pytest.fixture
def sample_chat_request():
    """示例请求"""
    from gateway.models import ChatCompletionRequest, ChatMessage
    return ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Hello, world!")],
        max_tokens=100,
        stream=False,
    )


# ============================================================
# 路由器测试
# ============================================================

class TestRouter:
    """路由器测试"""
    
    def test_round_robin(self, mock_worker_states):
        """测试轮询路由"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        # 轮询应该均匀分布
        results = []
        for _ in range(6):
            selected = router.select_worker(
                Mock(get_hash_key=lambda: "test"),
                strategy=RoutingStrategy.ROUND_ROBIN
            )
            results.append(selected)
        
        # 检查是否轮询
        assert len(set(results)) == 2  # 只有2个健康的worker
    
    def test_least_queue(self, mock_worker_states):
        """测试最短队列路由"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        selected = router.select_worker(
            Mock(get_hash_key=lambda: "test"),
            strategy=RoutingStrategy.LEAST_QUEUE
        )
        
        # 应该选择负载最低的 worker1 (running=5, waiting=2)
        assert selected == "http://worker1:8000"
    
    def test_p2c_kv_aware(self, mock_worker_states, sample_chat_request):
        """测试P2C-KV路由"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        # 多次选择，应该倾向于负载低的
        selections = []
        for _ in range(100):
            selected = router.select_worker(
                sample_chat_request,
                strategy=RoutingStrategy.P2C_KV_AWARE
            )
            selections.append(selected)
        
        # worker1 应该被选中更多次（因为负载更低）
        worker1_count = selections.count("http://worker1:8000")
        worker2_count = selections.count("http://worker2:8000")
        
        assert worker1_count > worker2_count
    
    def test_exclude_workers(self, mock_worker_states):
        """测试排除Worker"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        selected = router.select_worker(
            Mock(get_hash_key=lambda: "test"),
            strategy=RoutingStrategy.ROUND_ROBIN,
            excluded_workers=["http://worker1:8000"]
        )
        
        assert selected == "http://worker2:8000"
    
    def test_consistent_hash(self, mock_worker_states):
        """测试一致性哈希"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        # 相同的key应该路由到相同的worker
        mock_request = Mock(get_hash_key=lambda: "user123")
        
        results = set()
        for _ in range(10):
            selected = router.select_worker(
                mock_request,
                strategy=RoutingStrategy.CONSISTENT_HASH
            )
            results.add(selected)
        
        # 相同key应该总是路由到同一个worker
        assert len(results) == 1


class TestConsistentHashRing:
    """一致性哈希环测试"""
    
    def test_add_remove_node(self):
        """测试添加和移除节点"""
        from gateway.router import ConsistentHashRing
        
        ring = ConsistentHashRing(replicas=100)
        
        ring.add_node("node1")
        ring.add_node("node2")
        ring.add_node("node3")
        
        assert len(ring.nodes) == 3
        assert len(ring.sorted_keys) == 300
        
        ring.remove_node("node2")
        
        assert len(ring.nodes) == 2
        assert len(ring.sorted_keys) == 200
    
    def test_distribution(self):
        """测试分布均匀性"""
        from gateway.router import ConsistentHashRing
        
        ring = ConsistentHashRing(replicas=150)
        
        for i in range(5):
            ring.add_node(f"node{i}")
        
        # 分配1000个key
        distribution = {f"node{i}": 0 for i in range(5)}
        for i in range(1000):
            node = ring.get_node(f"key{i}")
            distribution[node] += 1
        
        # 检查分布是否相对均匀（允许20%偏差）
        avg = 1000 / 5
        for count in distribution.values():
            assert 0.8 * avg <= count <= 1.2 * avg


class TestAdaptiveRouter:
    """自适应路由器测试"""
    
    def test_learning(self):
        """测试学习功能"""
        from gateway.router import AdaptiveRouter
        from gateway.models import WorkerState, WorkerStatus
        
        router = AdaptiveRouter(epsilon=0.0)  # 禁用探索
        
        workers = {
            "worker1": WorkerState(url="worker1", running=0, waiting=0),
            "worker2": WorkerState(url="worker2", running=0, waiting=0),
        }
        
        # 训练：worker1 表现好，worker2 表现差
        for _ in range(50):
            router.record_result("worker1", latency_ms=100, success=True)
            router.record_result("worker2", latency_ms=500, success=False)
        
        # 验证学习效果
        selections = []
        for _ in range(20):
            selected = router.select(workers)
            selections.append(selected)
        
        # worker1 应该被选中更多
        assert selections.count("worker1") > selections.count("worker2")


# ============================================================
# 熔断器测试
# ============================================================

class TestCircuitBreaker:
    """熔断器测试"""
    
    def test_state_transitions(self):
        """测试状态转换"""
        from gateway.circuit_breaker import CircuitBreaker
        from gateway.models import CircuitState
        
        cb = CircuitBreaker(
            name="test",
            failure_threshold=3,
            success_threshold=2,
            timeout_seconds=0.1,
        )
        
        # 初始状态应该是 CLOSED
        assert cb.state == CircuitState.CLOSED
        
        # 连续失败触发熔断
        for _ in range(3):
            cb.record_failure("error")
        
        assert cb.state == CircuitState.OPEN
        
        # 等待超时，转换到 HALF_OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        
        # 成功恢复
        cb.record_success(100)
        cb.record_success(100)
        
        assert cb.state == CircuitState.CLOSED
    
    def test_allow_request(self):
        """测试请求允许"""
        from gateway.circuit_breaker import CircuitBreaker
        
        cb = CircuitBreaker(
            name="test",
            failure_threshold=2,
            half_open_max_requests=1,
        )
        
        # CLOSED 状态允许请求
        assert cb.allow_request() is True
        
        # 触发熔断
        cb.record_failure("error")
        cb.record_failure("error")
        
        # OPEN 状态拒绝请求
        assert cb.allow_request() is False
    
    def test_statistics(self):
        """测试统计功能"""
        from gateway.circuit_breaker import CircuitBreaker
        
        cb = CircuitBreaker(name="test")
        
        for _ in range(5):
            cb.record_success(100)
        for _ in range(3):
            cb.record_failure("error")
        
        stats = cb.get_statistics()
        
        assert stats["total_calls"] == 8
        assert stats["total_successes"] == 5
        assert stats["total_failures"] == 3


class TestCircuitBreakerManager:
    """熔断器管理器测试"""
    
    def test_get_breaker(self):
        """测试获取熔断器"""
        from gateway.circuit_breaker import CircuitBreakerManager
        
        manager = CircuitBreakerManager()
        
        cb1 = manager.get_breaker("worker1")
        cb2 = manager.get_breaker("worker1")
        cb3 = manager.get_breaker("worker2")
        
        # 同一个worker应该返回同一个熔断器
        assert cb1 is cb2
        assert cb1 is not cb3
    
    def test_get_open_breakers(self):
        """测试获取打开的熔断器"""
        from gateway.circuit_breaker import CircuitBreakerManager
        
        manager = CircuitBreakerManager()
        
        # 触发 worker1 熔断
        for _ in range(10):
            manager.record_failure("worker1", "error")
        
        open_breakers = manager.get_open_breakers()
        assert "worker1" in open_breakers


# ============================================================
# 限流器测试
# ============================================================

class TestTokenBucket:
    """令牌桶测试"""
    
    def test_basic_acquire(self):
        """测试基本获取"""
        from gateway.rate_limiter import TokenBucket
        
        bucket = TokenBucket(rate=10, capacity=10)
        
        # 应该能获取10个令牌
        for _ in range(10):
            allowed, _ = bucket.acquire()
            assert allowed is True
        
        # 第11个应该失败
        allowed, wait_time = bucket.acquire()
        assert allowed is False
        assert wait_time > 0
    
    def test_refill(self):
        """测试令牌补充"""
        from gateway.rate_limiter import TokenBucket
        
        bucket = TokenBucket(rate=100, capacity=10)
        
        # 消耗所有令牌
        for _ in range(10):
            bucket.acquire()
        
        # 等待补充
        time.sleep(0.1)  # 应该补充10个令牌
        
        allowed, _ = bucket.acquire()
        assert allowed is True


class TestSlidingWindowCounter:
    """滑动窗口测试"""
    
    def test_window_limit(self):
        """测试窗口限制"""
        from gateway.rate_limiter import SlidingWindowCounter
        
        counter = SlidingWindowCounter(limit=5, window_size=1.0)
        
        # 应该能通过5个请求
        for _ in range(5):
            allowed, _ = counter.acquire()
            assert allowed is True
        
        # 第6个应该被拒绝
        allowed, _ = counter.acquire()
        assert allowed is False


class TestRateLimiter:
    """限流器测试"""
    
    def test_check(self):
        """测试限流检查"""
        from gateway.rate_limiter import RateLimiter
        
        limiter = RateLimiter(requests_per_second=10, burst_size=10)
        
        # 前10个请求应该通过
        for _ in range(10):
            result = limiter.check()
            assert result.allowed is True
        
        # 第11个应该被限流
        result = limiter.check()
        assert result.allowed is False


# ============================================================
# 健康检查测试
# ============================================================

class TestHealthChecker:
    """健康检查器测试"""
    
    @pytest.mark.asyncio
    async def test_status_transition(self):
        """测试状态转换"""
        from gateway.health import HealthChecker
        from gateway.models import WorkerStatus
        from gateway.metrics import metrics_collector
        
        checker = HealthChecker()
        
        # 模拟 worker
        url = "http://test:8000"
        metrics_collector.workers[url] = Mock(
            url=url,
            status=WorkerStatus.HEALTHY,
            consecutive_failures=0,
            consecutive_successes=0,
            last_failure_reason="",
            last_health_check=0,
        )
        
        # 模拟失败处理
        worker = metrics_collector.workers[url]
        for _ in range(3):
            checker._handle_failure(worker, "connection refused")
        
        # 应该变为不健康
        assert worker.status == WorkerStatus.UNHEALTHY


# ============================================================
# 指标采集测试
# ============================================================

class TestMetricsCollector:
    """指标采集器测试"""
    
    def test_latency_tracker(self):
        """测试延迟追踪器"""
        from gateway.metrics import LatencyTracker
        
        tracker = LatencyTracker(window_size=100)
        
        # 添加一些延迟数据
        for i in range(100):
            tracker.record(i * 10)  # 0-990ms
        
        stats = tracker.get_stats()
        
        assert stats["count"] == 100
        assert stats["avg"] == 495  # 平均值
        assert stats["p50"] == 490  # 中位数
        assert stats["p95"] == 940  # P95
    
    def test_throughput_tracker(self):
        """测试吞吐量追踪器"""
        from gateway.metrics import ThroughputTracker
        
        tracker = ThroughputTracker(window_seconds=1.0)
        
        # 记录10个请求
        for _ in range(10):
            tracker.record(tokens=100)
        
        stats = tracker.get_stats()
        
        assert stats["samples"] == 10
        assert stats["qps"] > 0
        assert stats["tps"] > 0


# ============================================================
# 集成测试
# ============================================================

class TestIntegration:
    """集成测试"""
    
    @pytest.mark.asyncio
    async def test_app_startup(self):
        """测试应用启动"""
        from fastapi.testclient import TestClient
        from gateway.app import app
        
        # 注意：这需要模拟后台服务
        # 实际测试时需要更完整的设置
        pass
    
    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """测试健康端点"""
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app
        
        # 使用 httpx 测试
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            # 跳过需要后台服务的测试
            pass


# ============================================================
# 性能测试
# ============================================================

class TestPerformance:
    """性能测试"""
    
    def test_router_performance(self, mock_worker_states):
        """测试路由器性能"""
        from gateway.router import Router
        from gateway.models import RoutingStrategy
        
        router = Router()
        router._metrics_collector = Mock()
        router._metrics_collector.get_healthy_workers.return_value = {
            k: v for k, v in mock_worker_states.items() 
            if v.status.value == "healthy"
        }
        
        mock_request = Mock(get_hash_key=lambda: "test", messages=[], max_tokens=100)
        
        # 测试10000次选择的性能
        start = time.time()
        for _ in range(10000):
            router.select_worker(mock_request, strategy=RoutingStrategy.P2C_KV_AWARE)
        elapsed = time.time() - start
        
        # 应该在1秒内完成（每次 < 0.1ms）
        assert elapsed < 1.0
        
        ops_per_sec = 10000 / elapsed
        print(f"\nRouter performance: {ops_per_sec:.0f} ops/sec")
    
    def test_circuit_breaker_performance(self):
        """测试熔断器性能"""
        from gateway.circuit_breaker import CircuitBreaker
        
        cb = CircuitBreaker(name="test")
        
        start = time.time()
        for _ in range(100000):
            cb.allow_request()
            cb.record_success(100)
        elapsed = time.time() - start
        
        ops_per_sec = 100000 / elapsed
        print(f"\nCircuit breaker performance: {ops_per_sec:.0f} ops/sec")
        
        # 应该能处理至少100k ops/sec
        assert ops_per_sec > 100000
    
    def test_rate_limiter_performance(self):
        """测试限流器性能"""
        from gateway.rate_limiter import TokenBucket
        
        bucket = TokenBucket(rate=1000000, capacity=1000000)
        
        start = time.time()
        for _ in range(100000):
            bucket.acquire()
        elapsed = time.time() - start
        
        ops_per_sec = 100000 / elapsed
        print(f"\nRate limiter performance: {ops_per_sec:.0f} ops/sec")
        
        assert ops_per_sec > 100000


# ============================================================
# 压力测试工具
# ============================================================

class LoadTester:
    """压力测试工具"""
    
    def __init__(self, target_url: str, concurrency: int = 10, duration: int = 10):
        self.target_url = target_url
        self.concurrency = concurrency
        self.duration = duration
        self.results = []
    
    async def run(self):
        """运行压力测试"""
        import httpx
        
        async def worker():
            async with httpx.AsyncClient() as client:
                start = time.time()
                while time.time() - start < self.duration:
                    req_start = time.time()
                    try:
                        response = await client.post(
                            f"{self.target_url}/v1/chat/completions",
                            json={
                                "model": "test",
                                "messages": [{"role": "user", "content": "Hello"}],
                                "max_tokens": 10,
                            }
                        )
                        latency = (time.time() - req_start) * 1000
                        self.results.append({
                            "latency_ms": latency,
                            "status": response.status_code,
                            "success": response.status_code == 200,
                        })
                    except Exception as e:
                        self.results.append({
                            "latency_ms": (time.time() - req_start) * 1000,
                            "status": 0,
                            "success": False,
                            "error": str(e),
                        })
        
        tasks = [worker() for _ in range(self.concurrency)]
        await asyncio.gather(*tasks)
    
    def report(self):
        """生成报告"""
        if not self.results:
            return "No results"
        
        total = len(self.results)
        success = sum(1 for r in self.results if r["success"])
        latencies = [r["latency_ms"] for r in self.results if r["success"]]
        
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            sorted_lat = sorted(latencies)
            p50 = sorted_lat[int(len(sorted_lat) * 0.5)]
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
            p99 = sorted_lat[min(int(len(sorted_lat) * 0.99), len(sorted_lat) - 1)]
        else:
            avg_latency = p50 = p95 = p99 = 0
        
        qps = total / self.duration
        
        return f"""
========== Load Test Report ==========
Duration: {self.duration}s
Concurrency: {self.concurrency}
Total Requests: {total}
Success Rate: {success/total*100:.1f}%
QPS: {qps:.1f}
Avg Latency: {avg_latency:.1f}ms
P50 Latency: {p50:.1f}ms
P95 Latency: {p95:.1f}ms
P99 Latency: {p99:.1f}ms
======================================
"""


# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
