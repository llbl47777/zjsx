"""
LLM Scheduler Pro - 主应用
Enterprise Edition v3.0

功能：
- OpenAI 兼容 API
- 智能负载均衡
- 流式传输
- 故障恢复
- 实时监控
"""

import asyncio
import time
import uuid
import logging
from typing import Optional, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

from .config import settings, config_manager
from .models import (
    ChatCompletionRequest, 
    RequestContext,
    WorkerStatus,
)
from .router import router
from .metrics import metrics_collector
from .health import health_checker
from .circuit_breaker import circuit_breaker_manager, CircuitOpenError
from .rate_limiter import rate_limiter_manager
from .websocket import ws_manager

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    
    # 创建 HTTP 客户端
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=settings.request_timeout,
        ),
        limits=httpx.Limits(
            max_connections=settings.connection_pool_size,
            max_keepalive_connections=settings.keepalive_connections,
        ),
    )
    
    # 启动后台服务
    await metrics_collector.start()
    await health_checker.start()
    await ws_manager.start()
    
    # 设置路由器的指标收集器
    router.set_metrics_collector(metrics_collector)
    
    # 注册事件回调
    health_checker.on_status_change(ws_manager.notify_worker_status_change)
    health_checker.on_alert(ws_manager.notify_alert)
    
    logger.info(f"Gateway ready on {settings.host}:{settings.port}")
    logger.info(f"Workers: {settings.worker_urls}")
    logger.info(f"Routing strategy: {settings.routing_strategy}")
    
    yield
    
    # 关闭服务
    logger.info("Shutting down...")
    await ws_manager.stop()
    await health_checker.stop()
    await metrics_collector.stop()
    await app.state.client.aclose()
    logger.info("Goodbye!")


# 创建应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Enterprise LLM Load Balancer with intelligent routing",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=settings.cors_methods,
    allow_headers=settings.cors_headers,
)


# ============================================================
# 核心 API
# ============================================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Chat Completions API (OpenAI 兼容)"""
    try:
        body = await request.json()
        chat_request = ChatCompletionRequest(**body)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Invalid request: {e}", "type": "invalid_request_error"}}
        )
    
    ctx = RequestContext(request_id=str(uuid.uuid4()), priority=chat_request.priority)
    
    # 限流检查
    if settings.rate_limit_enabled:
        user_id = request.headers.get("X-User-ID") or request.headers.get("Authorization")
        rate_result = rate_limiter_manager.check(user_id=user_id)
        if not rate_result.allowed:
            return JSONResponse(
                status_code=429,
                content={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
                headers={"Retry-After": str(int(rate_result.retry_after or 1))}
            )
    
    metrics_collector.record_request_start()
    headers = {"Content-Type": "application/json", "X-Request-ID": ctx.request_id}
    
    for key in ["Authorization", "X-User-ID"]:
        if key in request.headers:
            headers[key] = request.headers[key]
    
    # 重试循环
    for attempt in range(settings.max_retries + 1):
        ctx.retry_count = attempt
        
        worker_url = router.select_worker(chat_request, excluded_workers=ctx.excluded_workers)
        if not worker_url:
            return JSONResponse(status_code=503, content={"error": {"message": "No healthy workers"}})
        
        ctx.selected_worker = worker_url
        
        if settings.circuit_breaker_enabled and not circuit_breaker_manager.allow_request(worker_url):
            ctx.excluded_workers.append(worker_url)
            continue
        
        try:
            if chat_request.stream:
                return await _handle_stream(app.state.client, worker_url, body, headers, ctx)
            else:
                return await _handle_request(app.state.client, worker_url, body, headers, ctx)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"Worker {worker_url} failed: {e}")
            circuit_breaker_manager.record_failure(worker_url, str(e))
            ctx.excluded_workers.append(worker_url)
        except Exception as e:
            logger.error(f"Error with {worker_url}: {e}")
            circuit_breaker_manager.record_failure(worker_url, str(e))
            ctx.excluded_workers.append(worker_url)
        
        if attempt < settings.max_retries:
            await asyncio.sleep(settings.retry_delay * (2 ** attempt))
    
    metrics_collector.record_request_end(ctx.selected_worker or "unknown", ctx.elapsed_ms(), success=False)
    return JSONResponse(status_code=502, content={"error": {"message": "All workers failed"}})


async def _handle_request(client, worker_url, body, headers, ctx):
    """处理非流式请求"""
    start = time.time()
    response = await client.post(f"{worker_url}/v1/chat/completions", json=body, headers=headers)
    latency_ms = (time.time() - start) * 1000
    
    if response.status_code == 200:
        circuit_breaker_manager.record_success(worker_url, latency_ms)
        router.record_result(worker_url, latency_ms, True)
        try:
            tokens = response.json().get("usage", {}).get("total_tokens", 0)
        except:
            tokens = 0
        metrics_collector.record_request_end(worker_url, latency_ms, tokens=tokens, success=True)
        return Response(content=response.content, status_code=200, headers={
            "Content-Type": "application/json", "X-Request-ID": ctx.request_id, "X-Worker-URL": worker_url
        })
    else:
        circuit_breaker_manager.record_failure(worker_url, f"HTTP {response.status_code}")
        raise Exception(f"Worker returned {response.status_code}")


async def _handle_stream(client, worker_url, body, headers, ctx):
    """处理流式请求"""
    async def generator():
        start = time.time()
        first_token = None
        tokens = 0
        try:
            async with client.stream("POST", f"{worker_url}/v1/chat/completions", json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    circuit_breaker_manager.record_failure(worker_url, f"HTTP {resp.status_code}")
                    yield await resp.aread()
                    return
                async for chunk in resp.aiter_bytes():
                    if first_token is None:
                        first_token = time.time()
                        ctx.ttft = (first_token - start) * 1000
                    if b'"content":' in chunk:
                        tokens += 1
                    yield chunk
            latency_ms = (time.time() - start) * 1000
            circuit_breaker_manager.record_success(worker_url, latency_ms)
            router.record_result(worker_url, latency_ms, True)
            metrics_collector.record_request_end(worker_url, latency_ms, ctx.ttft, tokens, True)
        except Exception as e:
            circuit_breaker_manager.record_failure(worker_url, str(e))
            metrics_collector.record_request_end(worker_url, (time.time()-start)*1000, success=False)
    
    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "X-Request-ID": ctx.request_id, "X-Worker-URL": worker_url
    })


@app.get("/v1/models")
async def list_models():
    """列出可用模型"""
    workers = metrics_collector.get_healthy_workers()
    if not workers:
        return JSONResponse(status_code=503, content={"error": {"message": "No healthy workers"}})
    url = list(workers.keys())[0]
    try:
        resp = await app.state.client.get(f"{url}/v1/models")
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": {"message": str(e)}})


# ============================================================
# 管理 API
# ============================================================

@app.get("/health")
async def health():
    summary = health_checker.get_status_summary()
    return JSONResponse(
        status_code=200 if summary["healthy"] > 0 else 503,
        content={"status": "healthy" if summary["healthy"] > 0 else "unhealthy", "workers": summary}
    )


@app.get("/metrics")
async def prometheus_metrics():
    return Response(content=metrics_collector.export_prometheus(), media_type="text/plain")


@app.get("/api/status")
async def get_status():
    gw = metrics_collector.get_gateway_metrics()
    ws = health_checker.get_status_summary()
    return {
        "app_name": settings.app_name, "version": settings.app_version,
        "uptime_seconds": gw.uptime_seconds, "routing_strategy": settings.routing_strategy,
        "workers": ws,
        "requests": {"total": gw.total_requests, "active": gw.active_requests, "per_second": gw.requests_per_second},
        "latency": {"avg_ms": gw.avg_latency_ms, "p95_ms": gw.p95_latency_ms, "p99_ms": gw.p99_latency_ms},
    }


@app.get("/api/workers")
async def get_workers():
    workers = []
    for url, state in metrics_collector.workers.items():
        workers.append({
            "url": url, "name": state.name, "status": state.status.value,
            "running": state.running, "waiting": state.waiting, "kv_cache_usage": state.kv_cache_usage,
            "total_requests": state.total_requests, "total_failures": state.total_failures,
            "avg_latency_ms": state.avg_latency_ms, "requests_per_second": state.requests_per_second,
            "circuit_state": circuit_breaker_manager.get_state(url).value,
        })
    return {"workers": workers}


@app.get("/api/workers/{worker_id}")
async def get_worker_detail(worker_id: int):
    urls = list(metrics_collector.workers.keys())
    if worker_id < 0 or worker_id >= len(urls):
        raise HTTPException(status_code=404, detail="Worker not found")
    url = urls[worker_id]
    return {
        "metrics": metrics_collector.get_worker_metrics(url).model_dump() if metrics_collector.get_worker_metrics(url) else None,
        "health": health_checker.get_worker_status(url),
        "circuit_breaker": circuit_breaker_manager.get_breaker(url).get_statistics(),
    }


@app.post("/api/workers/{worker_id}/drain")
async def drain_worker(worker_id: int):
    urls = list(metrics_collector.workers.keys())
    if worker_id < 0 or worker_id >= len(urls):
        raise HTTPException(status_code=404, detail="Worker not found")
    health_checker.drain_worker(urls[worker_id])
    return {"message": f"Worker {urls[worker_id]} is draining"}


@app.post("/api/workers/{worker_id}/undrain")
async def undrain_worker(worker_id: int):
    urls = list(metrics_collector.workers.keys())
    if worker_id < 0 or worker_id >= len(urls):
        raise HTTPException(status_code=404, detail="Worker not found")
    health_checker.undrain_worker(urls[worker_id])
    return {"message": f"Worker {urls[worker_id]} undrained"}


@app.post("/api/circuit-breaker/{worker_id}/reset")
async def reset_circuit_breaker(worker_id: int):
    urls = list(metrics_collector.workers.keys())
    if worker_id < 0 or worker_id >= len(urls):
        raise HTTPException(status_code=404, detail="Worker not found")
    circuit_breaker_manager.reset(urls[worker_id])
    return {"message": f"Circuit breaker reset"}


@app.get("/api/config")
async def get_config():
    return config_manager.get_snapshot()


@app.post("/api/config")
async def update_config(updates: dict):
    results = config_manager.update(updates)
    return {"success": all(results.values()), "results": results}


@app.get("/api/routing/stats")
async def get_routing_stats():
    return router.get_route_statistics()


@app.get("/api/rate-limit/stats")
async def get_rate_limit_stats():
    return rate_limiter_manager.get_stats()


@app.get("/api/circuit-breaker/stats")
async def get_circuit_breaker_stats():
    return circuit_breaker_manager.get_all_statistics()


# ============================================================
# WebSocket
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = str(uuid.uuid4())
    if not await ws_manager.connect(websocket, client_id):
        return
    try:
        while True:
            data = await websocket.receive_text()
            await ws_manager.handle_message(client_id, data)
    except WebSocketDisconnect:
        ws_manager.disconnect(client_id)


# ============================================================
# Web UI
# ============================================================

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LLM Scheduler Pro</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Inter', -apple-system, sans-serif; }
        .status-healthy { color: #10b981; }
        .status-unhealthy { color: #ef4444; }
        .status-recovering { color: #f59e0b; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <div class="container mx-auto px-4 py-8">
        <header class="mb-8">
            <div class="flex items-center justify-between">
                <div>
                    <h1 class="text-3xl font-bold text-blue-400">🚀 LLM Scheduler Pro</h1>
                    <p class="text-gray-400 mt-1">Enterprise Load Balancer Dashboard</p>
                </div>
                <div class="flex items-center space-x-4">
                    <span id="ws-status" class="px-3 py-1 rounded-full text-sm bg-red-500/20 text-red-400">● Disconnected</span>
                    <span id="uptime" class="text-gray-400 text-sm">Uptime: --</span>
                </div>
            </div>
        </header>

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <p class="text-gray-400 text-sm">Total Requests</p>
                <p id="total-requests" class="text-3xl font-bold text-white mt-1">0</p>
                <p id="qps" class="text-green-400 text-sm mt-2">0 req/s</p>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <p class="text-gray-400 text-sm">Active Requests</p>
                <p id="active-requests" class="text-3xl font-bold text-white mt-1">0</p>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <p class="text-gray-400 text-sm">Avg Latency</p>
                <p id="avg-latency" class="text-3xl font-bold text-white mt-1">0 ms</p>
                <p id="p95-latency" class="text-yellow-400 text-sm mt-2">P95: 0 ms</p>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <p class="text-gray-400 text-sm">Healthy Workers</p>
                <p id="healthy-workers" class="text-3xl font-bold text-green-400 mt-1">0/0</p>
                <p id="routing-strategy" class="text-blue-400 text-sm mt-2">Strategy: --</p>
            </div>
        </div>

        <div class="bg-gray-800 rounded-xl border border-gray-700 mb-8">
            <div class="px-6 py-4 border-b border-gray-700">
                <h2 class="text-xl font-semibold">Workers</h2>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full">
                    <thead class="bg-gray-700/50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Worker</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Running</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Waiting</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">KV Cache</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">QPS</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase">Latency</th>
                        </tr>
                    </thead>
                    <tbody id="workers-table" class="divide-y divide-gray-700"></tbody>
                </table>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <h3 class="text-lg font-semibold mb-4">Request Rate</h3>
                <canvas id="qps-chart" height="200"></canvas>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                <h3 class="text-lg font-semibold mb-4">Latency</h3>
                <canvas id="latency-chart" height="200"></canvas>
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let qpsHistory = [], latencyHistory = [];
        let qpsChart, latencyChart;

        document.addEventListener('DOMContentLoaded', () => {
            initCharts();
            connectWS();
        });

        function connectWS() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${proto}//${location.host}/ws`);
            ws.onopen = () => {
                document.getElementById('ws-status').innerHTML = '● Connected';
                document.getElementById('ws-status').className = 'px-3 py-1 rounded-full text-sm bg-green-500/20 text-green-400';
                ws.send(JSON.stringify({type: 'subscribe', topics: ['metrics', 'worker_status']}));
            };
            ws.onclose = () => {
                document.getElementById('ws-status').innerHTML = '● Disconnected';
                document.getElementById('ws-status').className = 'px-3 py-1 rounded-full text-sm bg-red-500/20 text-red-400';
                setTimeout(connectWS, 3000);
            };
            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                if (msg.type === 'metrics') updateMetrics(msg.data);
            };
        }

        function updateMetrics(data) {
            const gw = data.gateway;
            document.getElementById('total-requests').textContent = gw.total_requests.toLocaleString();
            document.getElementById('active-requests').textContent = gw.active_requests;
            document.getElementById('avg-latency').textContent = gw.avg_latency_ms.toFixed(0) + ' ms';
            document.getElementById('p95-latency').textContent = 'P95: ' + gw.p95_latency_ms.toFixed(0) + ' ms';
            document.getElementById('qps').textContent = gw.requests_per_second.toFixed(1) + ' req/s';
            document.getElementById('healthy-workers').textContent = gw.healthy_workers + '/' + gw.total_workers;
            document.getElementById('routing-strategy').textContent = 'Strategy: ' + gw.routing_strategy;
            document.getElementById('uptime').textContent = 'Uptime: ' + formatDuration(gw.uptime_seconds);

            qpsHistory.push(gw.requests_per_second);
            if (qpsHistory.length > 60) qpsHistory.shift();
            latencyHistory.push(gw.avg_latency_ms);
            if (latencyHistory.length > 60) latencyHistory.shift();
            qpsChart.data.datasets[0].data = qpsHistory;
            qpsChart.update('none');
            latencyChart.data.datasets[0].data = latencyHistory;
            latencyChart.update('none');

            if (data.workers) {
                const tbody = document.getElementById('workers-table');
                tbody.innerHTML = '';
                Object.entries(data.workers).forEach(([url, w], i) => {
                    tbody.innerHTML += `<tr class="hover:bg-gray-700/50">
                        <td class="px-6 py-4"><div class="text-sm font-medium">${w.name || 'Worker ' + i}</div><div class="text-xs text-gray-500">${url}</div></td>
                        <td class="px-6 py-4"><span class="status-${w.status} font-medium">${w.status.toUpperCase()}</span></td>
                        <td class="px-6 py-4 text-sm">${w.running}</td>
                        <td class="px-6 py-4 text-sm">${w.waiting}</td>
                        <td class="px-6 py-4"><div class="w-full bg-gray-700 rounded-full h-2"><div class="bg-blue-500 h-2 rounded-full" style="width:${(w.kv_cache_usage*100).toFixed(0)}%"></div></div><span class="text-xs text-gray-400">${(w.kv_cache_usage*100).toFixed(1)}%</span></td>
                        <td class="px-6 py-4 text-sm">${(w.requests_per_second||0).toFixed(1)}</td>
                        <td class="px-6 py-4 text-sm">${(w.avg_latency_ms||0).toFixed(0)} ms</td>
                    </tr>`;
                });
            }
        }

        function initCharts() {
            const chartOpts = {responsive: true, plugins: {legend: {display: false}}, scales: {y: {beginAtZero: true, grid: {color: '#374151'}}, x: {display: false}}};
            qpsChart = new Chart(document.getElementById('qps-chart').getContext('2d'), {
                type: 'line', data: {labels: Array(60).fill(''), datasets: [{data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, tension: 0.4}]}, options: chartOpts
            });
            latencyChart = new Chart(document.getElementById('latency-chart').getContext('2d'), {
                type: 'line', data: {labels: Array(60).fill(''), datasets: [{data: [], borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,0.1)', fill: true, tension: 0.4}]}, options: chartOpts
            });
        }

        function formatDuration(s) {
            const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
            return `${h}h ${m}m ${Math.floor(s%60)}s`;
        }
    </script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def root():
    return '<meta http-equiv="refresh" content="0; url=/ui" />'


@app.get("/ui", response_class=HTMLResponse)
@app.get("/ui/{path:path}", response_class=HTMLResponse)
async def serve_ui(path: str = ""):
    return DASHBOARD_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.app:app", host=settings.host, port=settings.port, reload=settings.debug)
