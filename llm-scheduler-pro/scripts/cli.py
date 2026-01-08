#!/usr/bin/env python3
"""
LLM Scheduler Pro - CLI 管理工具
Enterprise Edition v3.0

交互式终端管理工具，支持：
- 实时状态监控
- Worker 管理
- 配置调整
- 指标查看
- 日志查看
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Optional
import httpx

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Warning: 'rich' library not installed. Using basic output.")


# ============================================================
# 配置
# ============================================================

DEFAULT_GATEWAY_URL = "http://localhost:9000"


# ============================================================
# API 客户端
# ============================================================

class GatewayClient:
    """网关 API 客户端"""
    
    def __init__(self, base_url: str = DEFAULT_GATEWAY_URL):
        self.base_url = base_url.rstrip('/')
        self.client = httpx.Client(timeout=10.0)
    
    def get_status(self) -> dict:
        """获取网关状态"""
        resp = self.client.get(f"{self.base_url}/api/status")
        resp.raise_for_status()
        return resp.json()
    
    def get_workers(self) -> dict:
        """获取 Worker 列表"""
        resp = self.client.get(f"{self.base_url}/api/workers")
        resp.raise_for_status()
        return resp.json()
    
    def get_worker_detail(self, worker_id: int) -> dict:
        """获取 Worker 详情"""
        resp = self.client.get(f"{self.base_url}/api/workers/{worker_id}")
        resp.raise_for_status()
        return resp.json()
    
    def drain_worker(self, worker_id: int) -> dict:
        """下线 Worker"""
        resp = self.client.post(f"{self.base_url}/api/workers/{worker_id}/drain")
        resp.raise_for_status()
        return resp.json()
    
    def undrain_worker(self, worker_id: int) -> dict:
        """恢复 Worker"""
        resp = self.client.post(f"{self.base_url}/api/workers/{worker_id}/undrain")
        resp.raise_for_status()
        return resp.json()
    
    def reset_circuit_breaker(self, worker_id: int) -> dict:
        """重置熔断器"""
        resp = self.client.post(f"{self.base_url}/api/circuit-breaker/{worker_id}/reset")
        resp.raise_for_status()
        return resp.json()
    
    def get_config(self) -> dict:
        """获取配置"""
        resp = self.client.get(f"{self.base_url}/api/config")
        resp.raise_for_status()
        return resp.json()
    
    def update_config(self, updates: dict) -> dict:
        """更新配置"""
        resp = self.client.post(f"{self.base_url}/api/config", json=updates)
        resp.raise_for_status()
        return resp.json()
    
    def get_routing_stats(self) -> dict:
        """获取路由统计"""
        resp = self.client.get(f"{self.base_url}/api/routing/stats")
        resp.raise_for_status()
        return resp.json()
    
    def get_rate_limit_stats(self) -> dict:
        """获取限流统计"""
        resp = self.client.get(f"{self.base_url}/api/rate-limit/stats")
        resp.raise_for_status()
        return resp.json()
    
    def get_circuit_breaker_stats(self) -> dict:
        """获取熔断器统计"""
        resp = self.client.get(f"{self.base_url}/api/circuit-breaker/stats")
        resp.raise_for_status()
        return resp.json()
    
    def get_metrics(self) -> str:
        """获取 Prometheus 指标"""
        resp = self.client.get(f"{self.base_url}/metrics")
        resp.raise_for_status()
        return resp.text
    
    def health_check(self) -> dict:
        """健康检查"""
        resp = self.client.get(f"{self.base_url}/health")
        return {"status_code": resp.status_code, **resp.json()}
    
    def close(self):
        """关闭客户端"""
        self.client.close()


# ============================================================
# 命令实现
# ============================================================

def cmd_status(client: GatewayClient, args):
    """显示网关状态"""
    try:
        status = client.get_status()
        
        if RICH_AVAILABLE:
            console = Console()
            
            # 创建面板
            table = Table(box=box.ROUNDED, show_header=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="green")
            
            table.add_row("App Name", status["app_name"])
            table.add_row("Version", status["version"])
            table.add_row("Uptime", format_duration(status["uptime_seconds"]))
            table.add_row("Routing Strategy", status["routing_strategy"])
            table.add_row("", "")
            table.add_row("Total Requests", str(status["requests"]["total"]))
            table.add_row("Active Requests", str(status["requests"]["active"]))
            table.add_row("QPS", f"{status['requests']['per_second']:.1f}")
            table.add_row("", "")
            table.add_row("Avg Latency", f"{status['latency']['avg_ms']:.1f} ms")
            table.add_row("P95 Latency", f"{status['latency']['p95_ms']:.1f} ms")
            table.add_row("P99 Latency", f"{status['latency']['p99_ms']:.1f} ms")
            table.add_row("", "")
            table.add_row("Healthy Workers", f"{status['workers']['healthy']}/{status['workers']['total']}")
            table.add_row("Unhealthy Workers", str(status["workers"]["unhealthy"]))
            
            console.print(Panel(table, title="🚀 LLM Scheduler Pro Status", border_style="blue"))
        else:
            print(json.dumps(status, indent=2))
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_workers(client: GatewayClient, args):
    """显示 Worker 列表"""
    try:
        data = client.get_workers()
        workers = data.get("workers", [])
        
        if RICH_AVAILABLE:
            console = Console()
            
            table = Table(title="Workers", box=box.ROUNDED)
            table.add_column("ID", style="cyan")
            table.add_column("Name")
            table.add_column("URL", style="dim")
            table.add_column("Status")
            table.add_column("Running", justify="right")
            table.add_column("Waiting", justify="right")
            table.add_column("KV Cache", justify="right")
            table.add_column("QPS", justify="right")
            table.add_column("Latency", justify="right")
            table.add_column("Circuit")
            
            for i, w in enumerate(workers):
                status_color = {
                    "healthy": "green",
                    "unhealthy": "red",
                    "recovering": "yellow",
                    "draining": "dim",
                }.get(w["status"], "white")
                
                circuit_color = {
                    "closed": "green",
                    "open": "red",
                    "half_open": "yellow",
                }.get(w["circuit_state"], "white")
                
                table.add_row(
                    str(i),
                    w.get("name", f"worker-{i}"),
                    w["url"],
                    Text(w["status"].upper(), style=status_color),
                    str(w["running"]),
                    str(w["waiting"]),
                    f"{w['kv_cache_usage']*100:.1f}%",
                    f"{w['requests_per_second']:.1f}",
                    f"{w['avg_latency_ms']:.0f}ms",
                    Text(w["circuit_state"].upper(), style=circuit_color),
                )
            
            console.print(table)
        else:
            print(json.dumps(workers, indent=2))
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_worker_detail(client: GatewayClient, args):
    """显示 Worker 详情"""
    try:
        detail = client.get_worker_detail(args.id)
        
        if RICH_AVAILABLE:
            console = Console()
            console.print(Panel(
                json.dumps(detail, indent=2),
                title=f"Worker {args.id} Detail",
                border_style="blue"
            ))
        else:
            print(json.dumps(detail, indent=2))
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_drain(client: GatewayClient, args):
    """下线 Worker"""
    try:
        result = client.drain_worker(args.id)
        print(f"✓ {result['message']}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_undrain(client: GatewayClient, args):
    """恢复 Worker"""
    try:
        result = client.undrain_worker(args.id)
        print(f"✓ {result['message']}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_reset_cb(client: GatewayClient, args):
    """重置熔断器"""
    try:
        result = client.reset_circuit_breaker(args.id)
        print(f"✓ {result['message']}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_config(client: GatewayClient, args):
    """显示或更新配置"""
    try:
        if args.set:
            # 解析 key=value
            updates = {}
            for item in args.set:
                if '=' not in item:
                    print(f"Invalid format: {item} (expected key=value)")
                    continue
                key, value = item.split('=', 1)
                # 尝试解析 JSON 值
                try:
                    value = json.loads(value)
                except:
                    pass
                updates[key] = value
            
            if updates:
                result = client.update_config(updates)
                print(f"Config updated: {result}")
        else:
            config = client.get_config()
            
            if RICH_AVAILABLE:
                console = Console()
                console.print(Panel(
                    json.dumps(config["settings"], indent=2),
                    title="Configuration",
                    border_style="blue"
                ))
            else:
                print(json.dumps(config, indent=2))
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_routing(client: GatewayClient, args):
    """显示路由统计"""
    try:
        stats = client.get_routing_stats()
        
        if RICH_AVAILABLE:
            console = Console()
            
            table = Table(title="Routing Statistics", box=box.ROUNDED)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")
            
            table.add_row("Current Strategy", stats["current_strategy"])
            table.add_row("Total Requests", str(stats["total_requests"]))
            
            if stats.get("distribution"):
                table.add_row("", "")
                table.add_row("[bold]Distribution[/bold]", "")
                for url, data in stats["distribution"].items():
                    table.add_row(f"  {url}", f"{data['count']} ({data['percentage']})")
            
            console.print(table)
        else:
            print(json.dumps(stats, indent=2))
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_metrics(client: GatewayClient, args):
    """显示 Prometheus 指标"""
    try:
        metrics = client.get_metrics()
        print(metrics)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_health(client: GatewayClient, args):
    """健康检查"""
    try:
        result = client.health_check()
        
        status_icon = "✓" if result["status"] == "healthy" else "✗"
        color = "green" if result["status"] == "healthy" else "red"
        
        if RICH_AVAILABLE:
            console = Console()
            console.print(f"[{color}]{status_icon} Gateway is {result['status']}[/{color}]")
            console.print(f"  Healthy workers: {result['workers']['healthy']}/{result['workers']['total']}")
        else:
            print(f"{status_icon} Gateway is {result['status']}")
            print(f"  Healthy workers: {result['workers']['healthy']}/{result['workers']['total']}")
        
        sys.exit(0 if result["status"] == "healthy" else 1)
    
    except Exception as e:
        print(f"✗ Error: {e}")
        sys.exit(1)


def cmd_watch(client: GatewayClient, args):
    """实时监控"""
    if not RICH_AVAILABLE:
        print("Error: 'rich' library required for watch mode")
        print("Install with: pip install rich")
        sys.exit(1)
    
    console = Console()
    
    def generate_display():
        try:
            status = client.get_status()
            workers_data = client.get_workers()
            workers = workers_data.get("workers", [])
            
            # 创建布局
            layout = Layout()
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="stats", size=8),
                Layout(name="workers"),
            )
            
            # Header
            layout["header"].update(Panel(
                f"[bold blue]🚀 LLM Scheduler Pro[/bold blue] - {status['app_name']} v{status['version']}",
                style="blue"
            ))
            
            # Stats
            stats_table = Table(box=box.SIMPLE, show_header=False, expand=True)
            stats_table.add_column("", ratio=1)
            stats_table.add_column("", ratio=1)
            stats_table.add_column("", ratio=1)
            stats_table.add_column("", ratio=1)
            
            stats_table.add_row(
                f"[cyan]Requests:[/cyan] {status['requests']['total']:,}",
                f"[cyan]Active:[/cyan] {status['requests']['active']}",
                f"[cyan]QPS:[/cyan] {status['requests']['per_second']:.1f}",
                f"[cyan]Uptime:[/cyan] {format_duration(status['uptime_seconds'])}",
            )
            stats_table.add_row(
                f"[yellow]Avg Latency:[/yellow] {status['latency']['avg_ms']:.0f}ms",
                f"[yellow]P95:[/yellow] {status['latency']['p95_ms']:.0f}ms",
                f"[yellow]P99:[/yellow] {status['latency']['p99_ms']:.0f}ms",
                f"[green]Healthy:[/green] {status['workers']['healthy']}/{status['workers']['total']}",
            )
            
            layout["stats"].update(Panel(stats_table, title="Statistics"))
            
            # Workers table
            workers_table = Table(box=box.ROUNDED, expand=True)
            workers_table.add_column("ID", style="cyan", width=4)
            workers_table.add_column("URL", style="dim")
            workers_table.add_column("Status", width=12)
            workers_table.add_column("Running", justify="right", width=8)
            workers_table.add_column("Waiting", justify="right", width=8)
            workers_table.add_column("KV%", justify="right", width=6)
            workers_table.add_column("QPS", justify="right", width=8)
            workers_table.add_column("Latency", justify="right", width=10)
            
            for i, w in enumerate(workers):
                status_style = {
                    "healthy": "green",
                    "unhealthy": "red",
                    "recovering": "yellow",
                }.get(w["status"], "white")
                
                workers_table.add_row(
                    str(i),
                    w["url"],
                    Text(w["status"].upper(), style=status_style),
                    str(w["running"]),
                    str(w["waiting"]),
                    f"{w['kv_cache_usage']*100:.0f}%",
                    f"{w['requests_per_second']:.1f}",
                    f"{w['avg_latency_ms']:.0f}ms",
                )
            
            layout["workers"].update(Panel(workers_table, title="Workers"))
            
            return layout
        
        except Exception as e:
            return Panel(f"[red]Error: {e}[/red]")
    
    with Live(generate_display(), refresh_per_second=1, console=console) as live:
        try:
            while True:
                time.sleep(1)
                live.update(generate_display())
        except KeyboardInterrupt:
            pass


# ============================================================
# 辅助函数
# ============================================================

def format_duration(seconds: float) -> str:
    """格式化时长"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="LLM Scheduler Pro CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        "--url", "-u",
        default=DEFAULT_GATEWAY_URL,
        help=f"Gateway URL (default: {DEFAULT_GATEWAY_URL})"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # status
    subparsers.add_parser("status", help="Show gateway status")
    
    # workers
    subparsers.add_parser("workers", help="List all workers")
    
    # worker detail
    worker_parser = subparsers.add_parser("worker", help="Show worker detail")
    worker_parser.add_argument("id", type=int, help="Worker ID")
    
    # drain
    drain_parser = subparsers.add_parser("drain", help="Drain a worker")
    drain_parser.add_argument("id", type=int, help="Worker ID")
    
    # undrain
    undrain_parser = subparsers.add_parser("undrain", help="Undrain a worker")
    undrain_parser.add_argument("id", type=int, help="Worker ID")
    
    # reset-cb
    reset_cb_parser = subparsers.add_parser("reset-cb", help="Reset circuit breaker")
    reset_cb_parser.add_argument("id", type=int, help="Worker ID")
    
    # config
    config_parser = subparsers.add_parser("config", help="Show or update config")
    config_parser.add_argument("--set", "-s", nargs="+", help="Set config (key=value)")
    
    # routing
    subparsers.add_parser("routing", help="Show routing statistics")
    
    # metrics
    subparsers.add_parser("metrics", help="Show Prometheus metrics")
    
    # health
    subparsers.add_parser("health", help="Health check")
    
    # watch
    subparsers.add_parser("watch", help="Real-time monitoring")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    client = GatewayClient(args.url)
    
    try:
        commands = {
            "status": cmd_status,
            "workers": cmd_workers,
            "worker": cmd_worker_detail,
            "drain": cmd_drain,
            "undrain": cmd_undrain,
            "reset-cb": cmd_reset_cb,
            "config": cmd_config,
            "routing": cmd_routing,
            "metrics": cmd_metrics,
            "health": cmd_health,
            "watch": cmd_watch,
        }
        
        cmd_func = commands.get(args.command)
        if cmd_func:
            cmd_func(client, args)
        else:
            parser.print_help()
    
    finally:
        client.close()


if __name__ == "__main__":
    main()
