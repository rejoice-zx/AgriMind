# -*- coding: utf-8 -*-
"""
性能监控服务模块 - 追踪API调用耗时、向量检索性能等

功能:
- 性能指标收集
- 统计平均响应时间、成功率等
- P50、P95、P99百分位数计算
- 支持性能报告导出
"""

import time
import logging
import math
from functools import wraps
from collections import deque, Counter
from app.core.operation_observation import OUTCOMES
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional
from threading import Lock, RLock
from contextlib import contextmanager

from app.utils.time import utcnow

logger = logging.getLogger(__name__)

AGRI_REQUEST_METRICS = frozenset({"agri_decision_request", "agri_tool_request", "agri_archive_request",
    "agri_consultation_request", "agri_plan_request", "agri_diagnosis_request", "agri_expert_case_request", "agri_field_request"})
PUBLIC_METRICS = AGRI_REQUEST_METRICS | frozenset({"embedding_api", "vector_search", "keyword_search", "knowledge_add", "knowledge_update",
    "agri_decision_model", "agri_vision_model", "agri_decision_model_http", "agri_vision_model_http",
    "embedding_http", "agri_database_statement"}) | frozenset(
    "agri_tool_" + name for name in ("get_field_info", "get_weather", "get_farming_history", "search_agri_knowledge", "diagnose_crop_image", "calculate_gdd"))


@dataclass
class MetricRecord:
    """性能指标记录"""
    name: str
    duration: float  # 耗时（秒）
    success: bool
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    outcome: str = "success"


@dataclass
class MetricStats:
    """指标统计结果"""
    name: str
    count: int = 0
    success_rate: Optional[float] = None
    avg_duration: float = 0.0
    min_duration: float = 0.0
    max_duration: float = 0.0
    p50_duration: float = 0.0
    p95_duration: float = 0.0
    p99_duration: float = 0.0
    total_count: int = 0
    total_success: int = 0
    outcome_counts: dict = field(default_factory=dict)
    total_outcome_counts: dict = field(default_factory=dict)


class MetricCollector:
    """性能指标收集器"""
    
    def __init__(self, name: str, max_records: int = 1000):
        self.name = name
        self._max_records = max_records
        self._records: deque = deque(maxlen=max_records)
        self._lock = Lock()
        
        # 累计统计
        self._total_count = 0
        self._success_count = 0
        self._outcomes = Counter()
        self._total_duration = 0.0
    
    def record(self, duration: float, success: bool = True, metadata: dict = None, outcome=None):
        """记录一次指标"""
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            raise ValueError("性能耗时必须是有限的非负秒数")
        outcome = outcome or ("success" if success else "error")
        if outcome not in OUTCOMES: raise ValueError("Unknown operation outcome")
        record = MetricRecord(
            name=self.name,
            duration=duration,
            success=success,
            metadata=metadata or {}, outcome=outcome
        )
        
        with self._lock:
            self._records.append(record)
            self._total_count += 1
            self._outcomes[outcome] += 1
            if success:
                self._success_count += 1
            self._total_duration += duration
    
    @contextmanager
    def measure(self, metadata: dict = None):
        """测量代码块执行时间的上下文管理器"""
        start = time.perf_counter()
        success = True
        try:
            yield
        except BaseException:
            success = False
            raise
        finally:
            duration = time.perf_counter() - start
            self.record(duration, success, metadata)
    
    def get_stats(self, last_n: int = None) -> MetricStats:
        """获取统计信息
        
        Args:
            last_n: 只统计最近N条记录，None表示全部
        """
        with self._lock:
            if last_n:
                records = list(self._records)[-last_n:]
            else:
                records = list(self._records)
            total_count, total_success = self._total_count, self._success_count
            total_outcomes = dict(self._outcomes)
        
        if not records:
            return MetricStats(name=self.name, total_count=total_count, total_success=total_success, total_outcome_counts=total_outcomes)
        
        durations = [r.duration for r in records]
        success_count = sum(1 for r in records if r.success)
        
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        average = 0.0
        for index, duration in enumerate(durations, 1):
            average += (duration - average) / index
        
        return MetricStats(
            name=self.name,
            count=len(records),
            success_rate=success_count / len(records),
            avg_duration=average,
            min_duration=min(durations),
            max_duration=max(durations),
            p50_duration=durations_sorted[math.ceil(n * 0.50) - 1],
            p95_duration=durations_sorted[math.ceil(n * 0.95) - 1],
            p99_duration=durations_sorted[math.ceil(n * 0.99) - 1],
            total_count=total_count,
            total_success=total_success,
            outcome_counts=dict(Counter(record.outcome for record in records)), total_outcome_counts=total_outcomes
        )
    
    def clear(self):
        """清空记录"""
        with self._lock:
            self._records.clear()
            self._total_count = 0
            self._success_count = 0
            self._outcomes.clear()
            self._total_duration = 0.0


class PerformanceService:
    """性能监控服务（单例）"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        # 各类指标收集器
        self._lock = RLock()
        self._collectors: Dict[str, MetricCollector] = {name: MetricCollector(name) for name in sorted(PUBLIC_METRICS)}
        
        self._start_time = time.time()
        self._start_monotonic = time.monotonic()
        self._cleared_at = None
        logger.info("性能监控服务初始化完成")
    
    def get_collector(self, name: str) -> MetricCollector:
        """获取或创建指标收集器"""
        with self._lock:
            if name not in self._collectors:
                self._collectors[name] = MetricCollector(name)
            return self._collectors[name]
    
    @contextmanager
    def measure(self, metric_name: str, metadata: dict = None):
        """测量代码块执行时间"""
        started, success = time.perf_counter(), False
        try:
            yield
            success = True
        finally:
            self.record(metric_name, time.perf_counter() - started, success, metadata)
    
    def record(
        self, 
        metric_name: str, 
        duration: float, 
        success: bool = True, 
        metadata: dict = None,
        outcome=None
    ):
        """记录指标"""
        with self._lock:
            self.get_collector(metric_name).record(duration, success, metadata, outcome)
    
    def get_all_stats(self, last_n: int = None) -> Dict[str, MetricStats]:
        """获取所有指标统计"""
        with self._lock:
            return {name: collector.get_stats(last_n) for name, collector in self._collectors.items() if name in PUBLIC_METRICS}
    
    def get_summary(self) -> dict:
        """获取性能摘要"""
        with self._lock:
            return self._summary(self.get_all_stats())

    def snapshot(self, last_n=100):
        with self._lock:
            stats = self.get_all_stats(last_n)
            return self._summary(stats), stats

    def _summary(self, stats):
        uptime = time.monotonic() - self._start_monotonic
        total_requests = sum(s.total_count for name, s in stats.items() if name in AGRI_REQUEST_METRICS)
        total_success = sum(s.total_success for name, s in stats.items() if name in AGRI_REQUEST_METRICS)
        return {
            "uptime_seconds": uptime,
            "uptime_formatted": self._format_duration(uptime),
            "total_requests": total_requests,
            "degraded_requests": sum(s.total_outcome_counts.get("degraded", 0) for name, s in stats.items() if name in AGRI_REQUEST_METRICS),
            "process_id": __import__("os").getpid(),
            "retained_samples_per_metric": 1000,
            "overall_success_rate": total_success / total_requests if total_requests > 0 else None,
            "start_time": datetime.fromtimestamp(
                self._start_time, 
                tz=timezone.utc
            ).isoformat(),
            "cleared_at": self._cleared_at,
            "storage_scope": "current_process",
            "metric_scope": "agricultural_http_requests",
        }
    
    def _format_duration(self, seconds: float) -> str:
        """格式化时长"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        elif minutes > 0:
            return f"{minutes}分钟{secs}秒"
        else:
            return f"{secs}秒"
    
    def export_report(self) -> str:
        """导出性能报告"""
        summary, stats = self.snapshot(last_n=100)
        rate = "无请求样本" if summary['overall_success_rate'] is None else f"{summary['overall_success_rate']:.1%}"
        
        lines = [
            "=" * 60,
            "农业性能监控报告（当前后端进程）",
            f"生成时间: {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            f"运行时长: {summary['uptime_formatted']}",
            "=" * 60,
            "",
            f"已采集农业HTTP请求（监控启动／清空后累计）: {summary['total_requests']}",
            f"2xx完整响应比例（含鉴权及参数拒绝）: {rate}",
            "仅汇总一次外层HTTP请求；工具／检索调用不累加到请求数。不是模型或诊断正确率。",
            f"后端进程PID: {summary['process_id']}；检测到模型降级的2xx请求: {summary['degraded_requests']}",
            "数据库仅计农业请求中的cursor执行；不包含连接池等待／事务提交。模型HTTP计缓冲响应等待，Embedding另含执行器及重试。",
            "各项耗时统计最近100条（最多保留1000条），P50/P95/P99采用最近秩法；重启不保留性能采样。",
            "",
            "-" * 60,
            "各指标详情:",
            "-" * 60,
        ]
        
        for name, stat in stats.items():
            if stat.count > 0:
                lines.extend([
                    f"\n【{name}】",
                    f"  最近结果分类: {stat.outcome_counts}；累计结果分类: {stat.total_outcome_counts}",
                    f"  最近样本数: {stat.count}；累计调用数: {stat.total_count}",
                    f"  成功率: {stat.success_rate:.1%}",
                    f"  平均耗时: {stat.avg_duration*1000:.1f}ms",
                    f"  最小耗时: {stat.min_duration*1000:.1f}ms",
                    f"  最大耗时: {stat.max_duration*1000:.1f}ms",
                    f"  P50耗时: {stat.p50_duration*1000:.1f}ms",
                    f"  P95耗时: {stat.p95_duration*1000:.1f}ms",
                    f"  P99耗时: {stat.p99_duration*1000:.1f}ms",
                ])
        
        lines.append("\n" + "=" * 60)
        
        return "\n".join(lines)
    
    def clear_all(self):
        """清空所有记录"""
        with self._lock:
            for collector in self._collectors.values():
                collector.clear()
            self._cleared_at = utcnow().isoformat()
        logger.info("性能监控数据已清空")


# 全局单例
_performance_service: Optional[PerformanceService] = None


def get_performance_service() -> PerformanceService:
    """获取性能监控服务单例"""
    global _performance_service
    if _performance_service is None:
        _performance_service = PerformanceService()
    return _performance_service


def timed(metric_name: str):
    """性能计时装饰器"""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            service = get_performance_service()
            start = time.perf_counter()
            success = True
            try:
                return await func(*args, **kwargs)
            except BaseException:
                success = False
                raise
            finally:
                duration = time.perf_counter() - start
                service.record(metric_name, duration, success)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            service = get_performance_service()
            start = time.perf_counter()
            success = True
            try:
                return func(*args, **kwargs)
            except BaseException:
                success = False
                raise
            finally:
                duration = time.perf_counter() - start
                service.record(metric_name, duration, success)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator
