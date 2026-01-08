#!/usr/bin/env python3
"""
LLM Scheduler Pro - 压力测试工具
Enterprise Edition v3.0

支持：
- 并发请求
- 流式和非流式
- 自定义请求内容
- 详细报告
"""

import argparse
import asyncio
import time
import json
import random
import statistics
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import httpx


@dataclass
class RequestResult:
    """请求结果"""
    success: bool
    status_code: int
    latency_ms: float
    ttft_ms: Optional[float] = None
    tokens: int = 0
    error: Optional[str] = None


@dataclass
class LoadTestReport:
    """测试报告"""
    duration_seconds: float
    total_requests: int
    success_count: int
    failure_count: int
    qps: float
    latencies: List[float]
    errors: Dict[str, int]
    
    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_requests * 100 if self.total_requests > 0 else 0
    
    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0
    
    @property
    def p50_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_lat = sorted(self.latencies)
        return sorted_lat[int(len(sorted_lat) * 0.5)]
    
    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_lat = sorted(self.latencies)
        return sorted_lat[int(len(sorted_lat) * 0.95)]
    
    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0
        sorted_lat = sorted(self.latencies)
        return sorted_lat[min(int(len(sorted_lat) * 0.99), len(sorted_lat) - 1)]
    
    def to_string(self) -> str:
        return f"""
╔══════════════════════════════════════════════════════════════╗
║                    Load Test Report                          ║
╠══════════════════════════════════════════════════════════════╣
║  Duration:        {self.duration_seconds:>10.1f}s                           ║
║  Total Requests:  {self.total_requests:>10}                             ║
║  Success:         {self.success_count:>10}  ({self.success_rate:>5.1f}%)                    ║
║  Failures:        {self.failure_count:>10}                             ║
╠══════════════════════════════════════════════════════════════╣
║  Throughput:      {self.qps:>10.1f} req/s                         ║
╠══════════════════════════════════════════════════════════════╣
║  Latency (ms):                                               ║
║    Average:       {self.avg_latency:>10.1f}                             ║
║    P50:           {self.p50_latency:>10.1f}                             ║
║    P95:           {self.p95_latency:>10.1f}                             ║
║    P99:           {self.p99_latency:>10.1f}                             ║
║    Min:           {min(self.latencies) if self.latencies else 0:>10.1f}                             ║
║    Max:           {max(self.latencies) if self.latencies else 0:>10.1f}                             ║
╠══════════════════════════════════════════════════════════════╣
║  Errors:                                                     ║
{''.join(f'║    {k[:30]:<30} {v:>5}                         ║\n' for k, v in self.errors.items()) if self.errors else '║    (none)                                                    ║\n'}╚══════════════════════════════════════════════════════════════╝
"""


class LoadTester:
    """压力测试器"""
    
    def __init__(
        self,
        target_url: str,
        concurrency: int = 10,
        duration: int = 30,
        rate_limit: Optional[float] = None,
        stream: bool = False,
        model: str = "test-model",
    ):
        self.target_url = target_url.rstrip('/')
        self.concurrency = concurrency
        self.duration = duration
        self.rate_limit = rate_limit
        self.stream = stream
        self.model = model
        
        self.results: List[RequestResult] = []
        self.start_time: float = 0
        self.end_time: float = 0
        
        # 测试提示词
        self.prompts = [
            "Hello, how are you today?",
            "What is the capital of France?",
            "Explain quantum computing in simple terms.",
            "Write a short poem about the moon.",
            "What are the benefits of exercise?",
            "How does photosynthesis work?",
            "Describe the water cycle.",
            "What is machine learning?",
            "Explain the theory of relativity.",
            "What causes earthquakes?",
        ]
    
    def _get_random_request(self) -> dict:
        """生成随机请求"""
        return {
            "model": self.model,
            "messages": [
                {"role": "user", "content": random.choice(self.prompts)}
            ],
            "max_tokens": random.randint(50, 200),
            "temperature": 0.7,
            "stream": self.stream,
        }
    
    async def _send_request(self, client: httpx.AsyncClient) -> RequestResult:
        """发送单个请求"""
        request_data = self._get_random_request()
        start_time = time.time()
        ttft = None
        tokens = 0
        
        try:
            if self.stream:
                # 流式请求
                async with client.stream(
                    "POST",
                    f"{self.target_url}/v1/chat/completions",
                    json=request_data,
                    timeout=60.0,
                ) as response:
                    if response.status_code != 200:
                        return RequestResult(
                            success=False,
                            status_code=response.status_code,
                            latency_ms=(time.time() - start_time) * 1000,
                            error=f"HTTP {response.status_code}",
                        )
                    
                    first_chunk = True
                    async for chunk in response.aiter_bytes():
                        if first_chunk:
                            ttft = (time.time() - start_time) * 1000
                            first_chunk = False
                        if b'"content":' in chunk:
                            tokens += 1
                    
                    return RequestResult(
                        success=True,
                        status_code=200,
                        latency_ms=(time.time() - start_time) * 1000,
                        ttft_ms=ttft,
                        tokens=tokens,
                    )
            else:
                # 非流式请求
                response = await client.post(
                    f"{self.target_url}/v1/chat/completions",
                    json=request_data,
                    timeout=60.0,
                )
                
                latency_ms = (time.time() - start_time) * 1000
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        tokens = data.get("usage", {}).get("total_tokens", 0)
                    except:
                        pass
                    
                    return RequestResult(
                        success=True,
                        status_code=200,
                        latency_ms=latency_ms,
                        tokens=tokens,
                    )
                else:
                    return RequestResult(
                        success=False,
                        status_code=response.status_code,
                        latency_ms=latency_ms,
                        error=f"HTTP {response.status_code}",
                    )
        
        except httpx.TimeoutException:
            return RequestResult(
                success=False,
                status_code=0,
                latency_ms=(time.time() - start_time) * 1000,
                error="Timeout",
            )
        except httpx.ConnectError as e:
            return RequestResult(
                success=False,
                status_code=0,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Connection error: {e}",
            )
        except Exception as e:
            return RequestResult(
                success=False,
                status_code=0,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
            )
    
    async def _worker(self, client: httpx.AsyncClient, stop_event: asyncio.Event):
        """工作协程"""
        interval = 1.0 / self.rate_limit if self.rate_limit else 0
        
        while not stop_event.is_set():
            result = await self._send_request(client)
            self.results.append(result)
            
            if interval > 0:
                await asyncio.sleep(interval)
    
    async def run(self):
        """运行测试"""
        print(f"Starting load test...")
        print(f"  Target: {self.target_url}")
        print(f"  Concurrency: {self.concurrency}")
        print(f"  Duration: {self.duration}s")
        print(f"  Stream: {self.stream}")
        print()
        
        # 创建客户端
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            limits=httpx.Limits(max_connections=self.concurrency * 2),
        ) as client:
            stop_event = asyncio.Event()
            
            # 启动工作协程
            workers = [
                asyncio.create_task(self._worker(client, stop_event))
                for _ in range(self.concurrency)
            ]
            
            self.start_time = time.time()
            
            # 等待测试时间
            try:
                await asyncio.sleep(self.duration)
            except asyncio.CancelledError:
                pass
            
            # 停止工作协程
            stop_event.set()
            
            # 等待所有工作协程完成
            await asyncio.gather(*workers, return_exceptions=True)
            
            self.end_time = time.time()
    
    def get_report(self) -> LoadTestReport:
        """生成报告"""
        actual_duration = self.end_time - self.start_time
        
        success_count = sum(1 for r in self.results if r.success)
        failure_count = len(self.results) - success_count
        
        latencies = [r.latency_ms for r in self.results if r.success]
        
        errors: Dict[str, int] = {}
        for r in self.results:
            if not r.success and r.error:
                error_key = r.error[:50]
                errors[error_key] = errors.get(error_key, 0) + 1
        
        return LoadTestReport(
            duration_seconds=actual_duration,
            total_requests=len(self.results),
            success_count=success_count,
            failure_count=failure_count,
            qps=len(self.results) / actual_duration if actual_duration > 0 else 0,
            latencies=latencies,
            errors=errors,
        )


async def main():
    parser = argparse.ArgumentParser(description="LLM Scheduler Pro Load Tester")
    
    parser.add_argument(
        "url",
        nargs="?",
        default="http://localhost:9000",
        help="Target gateway URL (default: http://localhost:9000)"
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=10,
        help="Number of concurrent workers (default: 10)"
    )
    parser.add_argument(
        "-d", "--duration",
        type=int,
        default=30,
        help="Test duration in seconds (default: 30)"
    )
    parser.add_argument(
        "-r", "--rate",
        type=float,
        help="Rate limit per worker (requests/second)"
    )
    parser.add_argument(
        "-s", "--stream",
        action="store_true",
        help="Use streaming mode"
    )
    parser.add_argument(
        "-m", "--model",
        default="test-model",
        help="Model name (default: test-model)"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file for JSON report"
    )
    
    args = parser.parse_args()
    
    tester = LoadTester(
        target_url=args.url,
        concurrency=args.concurrency,
        duration=args.duration,
        rate_limit=args.rate,
        stream=args.stream,
        model=args.model,
    )
    
    await tester.run()
    
    report = tester.get_report()
    print(report.to_string())
    
    if args.output:
        with open(args.output, 'w') as f:
            json.dump({
                "duration_seconds": report.duration_seconds,
                "total_requests": report.total_requests,
                "success_count": report.success_count,
                "failure_count": report.failure_count,
                "success_rate": report.success_rate,
                "qps": report.qps,
                "avg_latency_ms": report.avg_latency,
                "p50_latency_ms": report.p50_latency,
                "p95_latency_ms": report.p95_latency,
                "p99_latency_ms": report.p99_latency,
                "errors": report.errors,
            }, f, indent=2)
        print(f"Report saved to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
