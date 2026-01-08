"""
LLM Scheduler Pro - 负载均衡路由模块
Enterprise Edition v3.0

支持的路由策略：
1. Round Robin (RR) - 轮询
2. Weighted Round Robin (WRR) - 加权轮询
3. Join Shortest Queue (JSQ) - 最短队列
4. Least Connections (LC) - 最少连接
5. Power of Two Choices with KV-aware (P2C-KV) - KV感知双选择
6. Consistent Hashing - 一致性哈希
7. Adaptive - 自适应学习
8. Random - 随机

创新点：
- P2C-KV 结合了 P2C 理论和 LLM 特有的 KV Cache 语义
- 自适应算法使用强化学习动态调整
- 一致性哈希支持会话亲和性
"""

import random
import time
import hashlib
import bisect
import logging
from typing import Optional, Dict, List, Tuple, Callable
from collections import deque
from dataclasses import dataclass, field
import threading
import math

from .models import (
    ChatCompletionRequest, 
    RoutingStrategy, 
    WorkerState,
    RequestPriority,
)
from .config import settings, get_worker_weight

logger = logging.getLogger(__name__)


# ============================================================
# 一致性哈希实现
# ============================================================

class ConsistentHashRing:
    """
    一致性哈希环
    
    用于实现会话亲和性，确保同一用户/会话的请求
    路由到同一个Worker（在该Worker健康的情况下）
    """
    
    def __init__(self, replicas: int = 150, hash_func: Optional[Callable] = None):
        self.replicas = replicas
        self.hash_func = hash_func or self._default_hash
        self.ring: Dict[int, str] = {}  # hash -> node
        self.sorted_keys: List[int] = []
        self.nodes: set = set()
        self._lock = threading.Lock()
    
    def _default_hash(self, key: str) -> int:
        """默认哈希函数（MD5）"""
        return int(hashlib.md5(key.encode()).hexdigest(), 16)
    
    def _get_node_key(self, node: str, replica: int) -> str:
        """生成虚拟节点的key"""
        return f"{node}:{replica}"
    
    def add_node(self, node: str, weight: float = 1.0):
        """添加节点"""
        with self._lock:
            if node in self.nodes:
                return
            
            self.nodes.add(node)
            # 根据权重调整虚拟节点数量
            num_replicas = int(self.replicas * weight)
            
            for i in range(num_replicas):
                key = self._get_node_key(node, i)
                hash_val = self.hash_func(key)
                self.ring[hash_val] = node
                bisect.insort(self.sorted_keys, hash_val)
    
    def remove_node(self, node: str):
        """移除节点"""
        with self._lock:
            if node not in self.nodes:
                return
            
            self.nodes.discard(node)
            
            # 移除所有虚拟节点
            keys_to_remove = [k for k, v in self.ring.items() if v == node]
            for key in keys_to_remove:
                del self.ring[key]
                self.sorted_keys.remove(key)
    
    def get_node(self, key: str) -> Optional[str]:
        """获取key对应的节点"""
        if not self.sorted_keys:
            return None
        
        hash_val = self.hash_func(key)
        
        # 二分查找第一个大于等于hash_val的位置
        idx = bisect.bisect_left(self.sorted_keys, hash_val)
        
        # 如果超过最大值，则环回到第一个
        if idx >= len(self.sorted_keys):
            idx = 0
        
        return self.ring[self.sorted_keys[idx]]
    
    def get_nodes(self, key: str, count: int = 1) -> List[str]:
        """获取key对应的多个节点（用于副本或故障转移）"""
        if not self.sorted_keys or count <= 0:
            return []
        
        hash_val = self.hash_func(key)
        idx = bisect.bisect_left(self.sorted_keys, hash_val)
        
        result = []
        seen = set()
        
        for i in range(len(self.sorted_keys)):
            curr_idx = (idx + i) % len(self.sorted_keys)
            node = self.ring[self.sorted_keys[curr_idx]]
            
            if node not in seen:
                seen.add(node)
                result.append(node)
                
                if len(result) >= count:
                    break
        
        return result
    
    def clear(self):
        """清空哈希环"""
        with self._lock:
            self.ring.clear()
            self.sorted_keys.clear()
            self.nodes.clear()


# ============================================================
# 自适应路由器
# ============================================================

@dataclass
class WorkerPerformance:
    """Worker 性能统计"""
    url: str
    window_size: int = 100
    
    # 滑动窗口
    latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    successes: deque = field(default_factory=lambda: deque(maxlen=100))
    
    # 统计值
    total_requests: int = 0
    total_success: int = 0
    total_latency: float = 0.0
    
    # 计算的得分
    score: float = 1.0
    
    def record(self, latency_ms: float, success: bool):
        """记录一次请求"""
        self.latencies.append(latency_ms)
        self.successes.append(1 if success else 0)
        self.total_requests += 1
        if success:
            self.total_success += 1
        self.total_latency += latency_ms
        self._update_score()
    
    def _update_score(self):
        """更新性能得分（越高越好）"""
        if len(self.latencies) == 0:
            self.score = 1.0
            return
        
        # 成功率 (0-1)
        success_rate = sum(self.successes) / len(self.successes)
        
        # 平均延迟的倒数（归一化）
        avg_latency = sum(self.latencies) / len(self.latencies)
        latency_score = 1000 / (avg_latency + 100)  # 避免除零
        
        # 综合得分
        self.score = success_rate * 0.7 + latency_score * 0.3
    
    @property
    def avg_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)
    
    @property
    def success_rate(self) -> float:
        if not self.successes:
            return 1.0
        return sum(self.successes) / len(self.successes)


class AdaptiveRouter:
    """
    自适应路由器
    
    使用 Epsilon-Greedy 策略：
    - 以 epsilon 概率随机探索
    - 以 1-epsilon 概率选择最佳 Worker
    
    性能得分基于：
    - 成功率
    - 响应延迟
    - 当前负载
    """
    
    def __init__(
        self, 
        epsilon: float = 0.1,
        learning_rate: float = 0.1,
        window_size: int = 100
    ):
        self.epsilon = epsilon
        self.learning_rate = learning_rate
        self.window_size = window_size
        self.performances: Dict[str, WorkerPerformance] = {}
        self._lock = threading.Lock()
    
    def select(
        self, 
        workers: Dict[str, WorkerState],
        excluded: Optional[List[str]] = None
    ) -> Optional[str]:
        """选择Worker"""
        excluded = excluded or []
        available = [url for url in workers.keys() if url not in excluded]
        
        if not available:
            return None
        
        # 确保所有Worker都有性能记录
        for url in available:
            if url not in self.performances:
                self.performances[url] = WorkerPerformance(
                    url=url, 
                    window_size=self.window_size
                )
        
        # Epsilon-Greedy
        if random.random() < self.epsilon:
            # 探索：随机选择
            return random.choice(available)
        else:
            # 利用：选择得分最高的
            scores = {}
            for url in available:
                perf = self.performances[url]
                state = workers[url]
                
                # 综合得分 = 历史性能 - 当前负载
                load_penalty = (state.running + state.waiting) * 0.1
                scores[url] = perf.score - load_penalty
            
            return max(available, key=lambda u: scores[u])
    
    def record_result(self, url: str, latency_ms: float, success: bool):
        """记录请求结果"""
        with self._lock:
            if url not in self.performances:
                self.performances[url] = WorkerPerformance(
                    url=url,
                    window_size=self.window_size
                )
            self.performances[url].record(latency_ms, success)
    
    def get_statistics(self) -> Dict[str, Dict]:
        """获取统计信息"""
        return {
            url: {
                "score": perf.score,
                "avg_latency_ms": perf.avg_latency,
                "success_rate": perf.success_rate,
                "total_requests": perf.total_requests,
            }
            for url, perf in self.performances.items()
        }


# ============================================================
# 主路由器
# ============================================================

class Router:
    """
    负载均衡路由器
    
    支持多种路由策略，可在运行时切换
    """
    
    def __init__(self):
        # 轮询计数器
        self.rr_counter = 0
        self.wrr_counters: Dict[str, int] = {}  # url -> current_weight
        
        # Tokenizer（用于估算token数）
        self.tokenizer = None
        self._init_tokenizer()
        
        # 一致性哈希环
        self.hash_ring = ConsistentHashRing(
            replicas=settings.hash_replicas
        )
        
        # 自适应路由器
        self.adaptive_router = AdaptiveRouter(
            epsilon=settings.adaptive_exploration_rate,
            learning_rate=settings.adaptive_learning_rate,
            window_size=settings.adaptive_window_size
        )
        
        # 统计
        self.route_counts: Dict[str, int] = {}
        self.strategy_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        
        # 指标收集器引用（延迟设置）
        self._metrics_collector = None
        
        logger.info(f"Router initialized with strategy: {settings.routing_strategy}")
    
    def set_metrics_collector(self, collector):
        """设置指标收集器"""
        self._metrics_collector = collector
    
    def _init_tokenizer(self):
        """初始化 tokenizer"""
        try:
            import tiktoken
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
            logger.info("Tokenizer initialized (tiktoken cl100k_base)")
        except Exception as e:
            logger.warning(f"Tokenizer not available: {e}")
    
    def get_healthy_workers(self) -> Dict[str, WorkerState]:
        """获取健康的Workers"""
        if self._metrics_collector:
            return self._metrics_collector.get_healthy_workers()
        return {}
    
    def select_worker(
        self, 
        request: ChatCompletionRequest,
        strategy: Optional[RoutingStrategy] = None,
        excluded_workers: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        选择一个 Worker 处理请求
        
        Args:
            request: 请求对象
            strategy: 路由策略（可选，默认使用配置）
            excluded_workers: 排除的 Worker 列表
        
        Returns:
            选中的 Worker URL
        """
        strategy = strategy or RoutingStrategy(settings.routing_strategy)
        healthy_workers = self.get_healthy_workers()
        
        # 排除指定的 Worker
        excluded_workers = excluded_workers or []
        healthy_workers = {
            url: state for url, state in healthy_workers.items()
            if url not in excluded_workers
        }
        
        if not healthy_workers:
            logger.warning("No healthy workers available")
            return None
        
        # 根据策略选择
        strategy_map = {
            RoutingStrategy.ROUND_ROBIN: self._round_robin,
            RoutingStrategy.WEIGHTED_ROUND_ROBIN: self._weighted_round_robin,
            RoutingStrategy.LEAST_QUEUE: self._least_queue,
            RoutingStrategy.LEAST_CONNECTIONS: self._least_connections,
            RoutingStrategy.P2C_KV_AWARE: self._p2c_kv_aware,
            RoutingStrategy.CONSISTENT_HASH: self._consistent_hash,
            RoutingStrategy.ADAPTIVE: self._adaptive,
            RoutingStrategy.RANDOM: self._random,
        }
        
        select_func = strategy_map.get(strategy, self._round_robin)
        
        if strategy in [RoutingStrategy.P2C_KV_AWARE, RoutingStrategy.CONSISTENT_HASH]:
            selected = select_func(healthy_workers, request)
        else:
            selected = select_func(healthy_workers)
        
        # 更新统计
        with self._lock:
            self.route_counts[selected] = self.route_counts.get(selected, 0) + 1
            strategy_name = strategy.value
            self.strategy_counts[strategy_name] = self.strategy_counts.get(strategy_name, 0) + 1
        
        return selected
    
    # ============================================================
    # 路由策略实现
    # ============================================================
    
    def _round_robin(self, workers: Dict[str, WorkerState]) -> str:
        """Round Robin 轮询"""
        urls = sorted(workers.keys())  # 排序确保顺序一致
        with self._lock:
            self.rr_counter = (self.rr_counter + 1) % len(urls)
            return urls[self.rr_counter]
    
    def _weighted_round_robin(self, workers: Dict[str, WorkerState]) -> str:
        """
        Weighted Round Robin 加权轮询
        
        使用平滑加权轮询算法：
        1. 每个节点有当前权重和有效权重
        2. 选择当前权重最大的节点
        3. 被选中的节点当前权重减去总权重
        4. 所有节点当前权重加上有效权重
        """
        urls = list(workers.keys())
        
        # 初始化计数器
        for url in urls:
            if url not in self.wrr_counters:
                self.wrr_counters[url] = 0
        
        # 获取权重
        weights = {url: get_worker_weight(url) for url in urls}
        total_weight = sum(weights.values())
        
        if total_weight == 0:
            return random.choice(urls)
        
        with self._lock:
            # 更新当前权重
            for url in urls:
                self.wrr_counters[url] += weights[url]
            
            # 选择当前权重最大的
            selected = max(urls, key=lambda u: self.wrr_counters[u])
            
            # 减去总权重
            self.wrr_counters[selected] -= total_weight
        
        return selected
    
    def _least_queue(self, workers: Dict[str, WorkerState]) -> str:
        """Join Shortest Queue (JSQ) - 最短队列"""
        return min(
            workers.keys(),
            key=lambda url: workers[url].running + workers[url].waiting
        )
    
    def _least_connections(self, workers: Dict[str, WorkerState]) -> str:
        """Least Connections - 最少连接"""
        return min(
            workers.keys(),
            key=lambda url: workers[url].running
        )
    
    def _p2c_kv_aware(
        self, 
        workers: Dict[str, WorkerState], 
        request: ChatCompletionRequest
    ) -> str:
        """
        Power of Two Choices with KV-aware (P2C-KV)
        
        核心思想：
        1. 随机选择 2 个 Worker
        2. 计算综合负载分数（考虑 KV Cache）
        3. 选择分数更低的 Worker
        """
        urls = list(workers.keys())
        
        # Power of Two Choices
        k = min(2, len(urls))
        candidates = random.sample(urls, k)
        
        if len(candidates) == 1:
            return candidates[0]
        
        # 估算 token 数
        prompt_tokens = self._estimate_prompt_tokens(request)
        max_output = request.max_tokens or 512
        
        def calculate_score(url: str) -> float:
            w = workers[url]
            
            # 基础负载分数
            score = (
                settings.weight_waiting * w.waiting +
                settings.weight_running * w.running +
                settings.weight_kv_cache * w.kv_cache_usage * 100 +
                settings.weight_prompt * prompt_tokens / 100 +
                settings.weight_output * max_output / 100
            )
            
            # 考虑历史延迟
            if settings.weight_latency > 0 and w.avg_latency_ms > 0:
                score += settings.weight_latency * w.avg_latency_ms / 100
            
            # 考虑错误率
            if settings.weight_error_rate > 0:
                error_rate = w.total_failures / max(w.total_requests, 1)
                score += settings.weight_error_rate * error_rate * 100
            
            return score
        
        scores = {url: calculate_score(url) for url in candidates}
        return min(candidates, key=lambda u: scores[u])
    
    def _consistent_hash(
        self, 
        workers: Dict[str, WorkerState],
        request: ChatCompletionRequest
    ) -> str:
        """一致性哈希"""
        # 更新哈希环
        current_nodes = set(workers.keys())
        ring_nodes = self.hash_ring.nodes
        
        # 添加新节点
        for url in current_nodes - ring_nodes:
            weight = get_worker_weight(url)
            self.hash_ring.add_node(url, weight)
        
        # 移除旧节点
        for url in ring_nodes - current_nodes:
            self.hash_ring.remove_node(url)
        
        # 获取hash key
        hash_key = request.get_hash_key()
        
        # 获取节点
        selected = self.hash_ring.get_node(hash_key)
        
        # 如果选中的节点不可用，获取下一个
        if selected not in workers:
            nodes = self.hash_ring.get_nodes(hash_key, count=len(workers))
            for node in nodes:
                if node in workers:
                    selected = node
                    break
        
        return selected or random.choice(list(workers.keys()))
    
    def _adaptive(self, workers: Dict[str, WorkerState]) -> str:
        """自适应路由"""
        return self.adaptive_router.select(workers)
    
    def _random(self, workers: Dict[str, WorkerState]) -> str:
        """随机选择"""
        return random.choice(list(workers.keys()))
    
    # ============================================================
    # 辅助方法
    # ============================================================
    
    def _estimate_prompt_tokens(self, request: ChatCompletionRequest) -> int:
        """估算 prompt token 数"""
        text = " ".join(m.content for m in request.messages)
        
        if self.tokenizer:
            try:
                return len(self.tokenizer.encode(text))
            except:
                pass
        
        # 粗略估计：混合语言约 2.5 字符/token
        return len(text) // 3
    
    def record_result(self, url: str, latency_ms: float, success: bool):
        """记录请求结果（用于自适应路由）"""
        self.adaptive_router.record_result(url, latency_ms, success)
    
    def update_hash_ring(self, workers: Dict[str, WorkerState]):
        """更新一致性哈希环"""
        current_nodes = set(workers.keys())
        ring_nodes = self.hash_ring.nodes
        
        for url in current_nodes - ring_nodes:
            self.hash_ring.add_node(url, get_worker_weight(url))
        
        for url in ring_nodes - current_nodes:
            self.hash_ring.remove_node(url)
    
    # ============================================================
    # 统计方法
    # ============================================================
    
    def get_route_statistics(self) -> Dict:
        """获取路由统计"""
        with self._lock:
            total = sum(self.route_counts.values())
            return {
                "total_requests": total,
                "current_strategy": settings.routing_strategy,
                "distribution": {
                    url: {
                        "count": count,
                        "percentage": f"{count/total*100:.1f}%" if total > 0 else "0%"
                    }
                    for url, count in self.route_counts.items()
                },
                "strategy_usage": self.strategy_counts.copy(),
                "adaptive_stats": self.adaptive_router.get_statistics(),
            }
    
    def reset_statistics(self):
        """重置统计"""
        with self._lock:
            self.route_counts.clear()
            self.strategy_counts.clear()
        logger.info("Router statistics reset")
    
    def get_hash_ring_info(self) -> Dict:
        """获取一致性哈希环信息"""
        return {
            "nodes": list(self.hash_ring.nodes),
            "virtual_nodes": len(self.hash_ring.sorted_keys),
            "replicas": self.hash_ring.replicas,
        }


# ============================================================
# 全局单例
# ============================================================

router = Router()
