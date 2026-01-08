"""
LLM Scheduler Pro - WebSocket 管理模块
Enterprise Edition v3.0

功能：
- 实时指标推送
- Worker 状态变更通知
- 告警推送
- 请求追踪
- 心跳保活
"""

import asyncio
import time
import json
import logging
from typing import Dict, Set, Optional, Any, List
from dataclasses import dataclass, field
from weakref import WeakSet
from fastapi import WebSocket, WebSocketDisconnect
import threading

from .models import WSMessageType, WSMessage, Alert, WorkerStatus
from .metrics import metrics_collector
from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class WSClient:
    """WebSocket 客户端"""
    websocket: WebSocket
    client_id: str
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    subscriptions: Set[WSMessageType] = field(default_factory=lambda: set(WSMessageType))
    
    # 统计
    messages_sent: int = 0
    messages_received: int = 0


class WebSocketManager:
    """
    WebSocket 连接管理器
    
    功能：
    - 连接管理
    - 消息广播
    - 订阅管理
    - 心跳检测
    """
    
    def __init__(self):
        self.clients: Dict[str, WSClient] = {}
        self._lock = threading.Lock()
        self._running = False
        self._broadcast_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        # 消息队列
        self._message_queue: asyncio.Queue = None
        
        logger.info("WebSocketManager initialized")
    
    async def start(self):
        """启动 WebSocket 服务"""
        self._running = True
        self._message_queue = asyncio.Queue()
        
        # 启动广播任务
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        
        # 启动心跳任务
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        # 启动指标推送任务
        asyncio.create_task(self._metrics_push_loop())
        
        logger.info("WebSocketManager started")
    
    async def stop(self):
        """停止 WebSocket 服务"""
        self._running = False
        
        # 关闭所有连接
        for client in list(self.clients.values()):
            try:
                await client.websocket.close()
            except:
                pass
        
        self.clients.clear()
        
        # 取消任务
        if self._broadcast_task:
            self._broadcast_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        logger.info("WebSocketManager stopped")
    
    async def connect(self, websocket: WebSocket, client_id: str) -> bool:
        """接受新连接"""
        # 检查连接数限制
        if len(self.clients) >= settings.ws_max_connections:
            logger.warning(f"Max WebSocket connections reached ({settings.ws_max_connections})")
            return False
        
        await websocket.accept()
        
        client = WSClient(
            websocket=websocket,
            client_id=client_id,
        )
        
        with self._lock:
            self.clients[client_id] = client
        
        logger.info(f"WebSocket client connected: {client_id}, total: {len(self.clients)}")
        
        # 发送欢迎消息
        await self._send_to_client(client, WSMessage(
            type=WSMessageType.HEARTBEAT,
            data={
                "message": "Connected to LLM Scheduler Pro",
                "client_id": client_id,
                "server_time": time.time(),
            }
        ))
        
        return True
    
    def disconnect(self, client_id: str):
        """断开连接"""
        with self._lock:
            if client_id in self.clients:
                del self.clients[client_id]
                logger.info(f"WebSocket client disconnected: {client_id}, remaining: {len(self.clients)}")
    
    async def handle_message(self, client_id: str, data: str):
        """处理客户端消息"""
        if client_id not in self.clients:
            return
        
        client = self.clients[client_id]
        client.messages_received += 1
        client.last_heartbeat = time.time()
        
        try:
            message = json.loads(data)
            msg_type = message.get("type", "")
            
            if msg_type == "subscribe":
                # 订阅消息类型
                topics = message.get("topics", [])
                for topic in topics:
                    try:
                        client.subscriptions.add(WSMessageType(topic))
                    except ValueError:
                        pass
                logger.debug(f"Client {client_id} subscribed to: {topics}")
            
            elif msg_type == "unsubscribe":
                # 取消订阅
                topics = message.get("topics", [])
                for topic in topics:
                    try:
                        client.subscriptions.discard(WSMessageType(topic))
                    except ValueError:
                        pass
            
            elif msg_type == "ping":
                # 心跳响应
                await self._send_to_client(client, WSMessage(
                    type=WSMessageType.HEARTBEAT,
                    data={"pong": True, "server_time": time.time()}
                ))
            
            elif msg_type == "get_metrics":
                # 立即获取指标
                await self._send_metrics(client)
            
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from client {client_id}")
    
    async def _send_to_client(self, client: WSClient, message: WSMessage):
        """发送消息给单个客户端"""
        try:
            await client.websocket.send_json(message.model_dump())
            client.messages_sent += 1
        except Exception as e:
            logger.debug(f"Failed to send to client {client.client_id}: {e}")
            self.disconnect(client.client_id)
    
    async def broadcast(self, message: WSMessage, msg_type: Optional[WSMessageType] = None):
        """广播消息给所有订阅的客户端"""
        msg_type = msg_type or message.type
        
        with self._lock:
            clients = list(self.clients.values())
        
        for client in clients:
            # 检查订阅
            if msg_type in client.subscriptions or not client.subscriptions:
                await self._send_to_client(client, message)
    
    async def send_to(self, client_id: str, message: WSMessage):
        """发送消息给指定客户端"""
        if client_id in self.clients:
            await self._send_to_client(self.clients[client_id], message)
    
    def queue_broadcast(self, message: WSMessage):
        """将消息加入广播队列（线程安全）"""
        if self._message_queue:
            try:
                self._message_queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Broadcast queue full, dropping message")
    
    async def _broadcast_loop(self):
        """广播循环"""
        while self._running:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=1.0
                )
                await self.broadcast(message)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Broadcast error: {e}")
    
    async def _heartbeat_loop(self):
        """心跳检测循环"""
        while self._running:
            try:
                await asyncio.sleep(settings.ws_heartbeat_interval)
                
                now = time.time()
                timeout = settings.ws_heartbeat_interval * 3
                
                # 检查超时客户端
                with self._lock:
                    timeout_clients = [
                        cid for cid, client in self.clients.items()
                        if now - client.last_heartbeat > timeout
                    ]
                
                for client_id in timeout_clients:
                    logger.info(f"Client {client_id} heartbeat timeout")
                    self.disconnect(client_id)
                
                # 发送心跳
                await self.broadcast(WSMessage(
                    type=WSMessageType.HEARTBEAT,
                    data={"server_time": now}
                ), WSMessageType.HEARTBEAT)
                
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def _metrics_push_loop(self):
        """指标推送循环"""
        while self._running:
            try:
                await asyncio.sleep(1.0)  # 每秒推送一次
                
                if not self.clients:
                    continue
                
                # 获取指标
                gateway_metrics = metrics_collector.get_gateway_metrics()
                worker_states = {
                    url: {
                        "url": w.url,
                        "name": w.name,
                        "status": w.status.value,
                        "running": w.running,
                        "waiting": w.waiting,
                        "kv_cache_usage": w.kv_cache_usage,
                        "requests_per_second": w.requests_per_second,
                        "avg_latency_ms": w.avg_latency_ms,
                    }
                    for url, w in metrics_collector.workers.items()
                }
                
                # 广播
                await self.broadcast(WSMessage(
                    type=WSMessageType.METRICS,
                    data={
                        "gateway": gateway_metrics.model_dump(),
                        "workers": worker_states,
                    }
                ), WSMessageType.METRICS)
                
            except Exception as e:
                logger.error(f"Metrics push error: {e}")
    
    async def _send_metrics(self, client: WSClient):
        """发送当前指标给指定客户端"""
        gateway_metrics = metrics_collector.get_gateway_metrics()
        worker_states = {
            url: {
                "url": w.url,
                "name": w.name,
                "status": w.status.value,
                "running": w.running,
                "waiting": w.waiting,
                "kv_cache_usage": w.kv_cache_usage,
            }
            for url, w in metrics_collector.workers.items()
        }
        
        await self._send_to_client(client, WSMessage(
            type=WSMessageType.METRICS,
            data={
                "gateway": gateway_metrics.model_dump(),
                "workers": worker_states,
            }
        ))
    
    # ============================================================
    # 事件通知方法
    # ============================================================
    
    def notify_worker_status_change(self, url: str, old_status: WorkerStatus, new_status: WorkerStatus):
        """通知 Worker 状态变更"""
        self.queue_broadcast(WSMessage(
            type=WSMessageType.WORKER_STATUS,
            data={
                "url": url,
                "old_status": old_status.value,
                "new_status": new_status.value,
            }
        ))
    
    def notify_alert(self, alert: Alert):
        """通知告警"""
        self.queue_broadcast(WSMessage(
            type=WSMessageType.ALERT,
            data=alert.model_dump()
        ))
    
    def notify_config_update(self, key: str, old_value: Any, new_value: Any):
        """通知配置更新"""
        self.queue_broadcast(WSMessage(
            type=WSMessageType.CONFIG_UPDATE,
            data={
                "key": key,
                "old_value": str(old_value),
                "new_value": str(new_value),
            }
        ))
    
    def notify_request_trace(self, trace_data: Dict):
        """通知请求追踪"""
        self.queue_broadcast(WSMessage(
            type=WSMessageType.REQUEST_TRACE,
            data=trace_data
        ))
    
    # ============================================================
    # 统计方法
    # ============================================================
    
    def get_stats(self) -> Dict:
        """获取 WebSocket 统计"""
        with self._lock:
            clients_info = [
                {
                    "client_id": c.client_id,
                    "connected_at": c.connected_at,
                    "last_heartbeat": c.last_heartbeat,
                    "messages_sent": c.messages_sent,
                    "messages_received": c.messages_received,
                    "subscriptions": [s.value for s in c.subscriptions],
                }
                for c in self.clients.values()
            ]
        
        return {
            "total_clients": len(clients_info),
            "max_connections": settings.ws_max_connections,
            "clients": clients_info,
        }


# ============================================================
# 全局单例
# ============================================================

ws_manager = WebSocketManager()
