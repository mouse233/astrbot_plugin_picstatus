from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil
from cpuinfo import get_cpu_info

from .http_client import get_http_client
from .utils import CpuFreq, readable_python_version, system_name
from .version_resolver import resolve_astrbot_version


def _dt_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


BOOT_TIME = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).astimezone()
ASTRBOT_START_TIME = _dt_now()
CPU_SAMPLE_INTERVAL = 0.1


def _format_td(dt: timedelta) -> str:
    days = dt.days
    rest = dt - timedelta(days=days)
    hours, rem = divmod(rest.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    parts.append(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
    return " ".join(parts)


_cpu_brand_cache: str | None = None


def get_cpu_brand() -> str:
    global _cpu_brand_cache
    if _cpu_brand_cache is not None:
        return _cpu_brand_cache
    try:
        brand = str(get_cpu_info().get("brand_raw") or "")
    except Exception:
        return "Unknown CPU"
    brand = brand.strip()
    if brand.lower().endswith(("cpu", "processor")):
        brand = brand.rsplit(" ", 1)[0]
    _cpu_brand_cache = brand
    return brand


def cpu_count() -> int | None:
    return psutil.cpu_count(logical=False)


def cpu_count_logical() -> int | None:
    return psutil.cpu_count()


def cpu_percent(interval: float = CPU_SAMPLE_INTERVAL) -> float:
    # 采集已放入线程池，短间隔采样能避免首次调用固定返回 0.0。
    return psutil.cpu_percent(interval=interval)


def cpu_freq() -> CpuFreq:
    freq = psutil.cpu_freq()
    return CpuFreq(
        current=getattr(freq, "current", None),
        min=getattr(freq, "min", None),
        max=getattr(freq, "max", None),
    )


@dataclass
class MemStat:
    total: int
    used: int
    percent: float


def memory_stat() -> MemStat:
    m = psutil.virtual_memory()
    return MemStat(total=m.total, used=m.used, percent=m.percent)


def swap_stat() -> MemStat:
    s = psutil.swap_memory()
    return MemStat(total=s.total, used=s.used, percent=s.percent)


@dataclass
class DiskUsage:
    name: str
    used: int | None
    total: int | None
    percent: float | None
    exception: str | None = None


def disk_usage(
    ignore: list[str] | None = None, max_items: int | None = None
) -> list[DiskUsage]:
    ignore = ignore or []
    ret: list[DiskUsage] = []
    for part in psutil.disk_partitions(all=False):
        name = part.mountpoint
        if any(x in name for x in ignore):
            continue
        try:
            u = psutil.disk_usage(name)
            ret.append(
                DiskUsage(name=name, used=u.used, total=u.total, percent=u.percent)
            )
        except Exception as e:
            ret.append(
                DiskUsage(name=name, used=None, total=None, percent=None, exception=str(e)),
            )
    ret.sort(key=lambda x: x.percent if x.percent is not None else -1, reverse=True)
    if max_items is not None:
        ret = ret[:max_items]
    return ret


@dataclass
class DiskIO:
    name: str
    read: float
    write: float


_last_disk_io = (time.time(), psutil.disk_io_counters(perdisk=True))


def disk_io() -> list[DiskIO]:
    global _last_disk_io
    now = time.time()
    past_t, past = _last_disk_io
    now_c = psutil.disk_io_counters(perdisk=True)
    dt = max(1e-6, now - past_t)
    ret: list[DiskIO] = []
    for name, now_one in now_c.items():
        if name not in past:
            continue
        past_one = past[name]
        read = max(0.0, (now_one.read_bytes - past_one.read_bytes) / dt)
        write = max(0.0, (now_one.write_bytes - past_one.write_bytes) / dt)
        ret.append(DiskIO(name=name, read=read, write=write))
    _last_disk_io = (now, now_c)
    # Top a few entries for readability
    ret.sort(key=lambda x: (x.read + x.write), reverse=True)
    return ret[:6]


@dataclass
class NetIO:
    name: str
    sent: float
    recv: float


_last_net_io = (time.time(), psutil.net_io_counters(pernic=True))


def network_io(ignore_names: list[str] | None = None) -> list[NetIO]:
    global _last_net_io
    ignore_names = ignore_names or []
    now = time.time()
    past_t, past = _last_net_io
    now_c = psutil.net_io_counters(pernic=True)
    dt = max(1e-6, now - past_t)
    ret: list[NetIO] = []
    for name, now_one in now_c.items():
        if any(Path(name).match(pat) for pat in ignore_names):
            continue
        if name not in past:
            continue
        past_one = past[name]
        sent = max(0.0, (now_one.bytes_sent - past_one.bytes_sent) / dt)
        recv = max(0.0, (now_one.bytes_recv - past_one.bytes_recv) / dt)
        ret.append(NetIO(name=name, sent=sent, recv=recv))
    _last_net_io = (now, now_c)
    ret.sort(key=lambda x: (x.sent + x.recv), reverse=True)
    return ret[:6]


@dataclass
class ConnTest:
    name: str
    status: str
    reason: str
    delay: float
    error: str | None = None


async def _connection_check(
    cli: httpx.AsyncClient, name: str, url: str
) -> ConnTest:
    start = time.perf_counter()
    try:
        resp = await cli.get(
            url,
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            headers={"User-Agent": "AstrBot-PicStatus/1.0"},
        )
        dt = (time.perf_counter() - start) * 1000
        return ConnTest(
            name=name,
            status=str(resp.status_code),
            reason="OK" if resp.status_code == 204 else (resp.reason_phrase or "HTTP"),
            delay=dt,
        )
    except Exception as e:
        dt = (time.perf_counter() - start) * 1000
        return ConnTest(
            name=name,
            status="ERR",
            reason="",
            delay=dt,
            error=f"{e.__class__.__name__}: {e}",
        )


async def connection_test() -> list[ConnTest]:
    endpoints = [
        ("Google", "https://www.gstatic.com/generate_204"),
        ("Cloudflare", "https://cp.cloudflare.com/generate_204"),
        ("Xiaomi", "http://connect.rom.miui.com/generate_204"),
    ]
    cli = await get_http_client()
    results = await asyncio.gather(
        *(_connection_check(cli, name, url) for name, url in endpoints)
    )
    return list(results)


@dataclass
class ProcStatus:
    name: str
    cpu: float
    mem: int


def process_status(
    n: int = 5,
    memory_scan_limit: int = 20,
    sample_interval: float = CPU_SAMPLE_INTERVAL,
) -> list[ProcStatus]:
    processes: list[tuple[psutil.Process, str]] = []
    for p in psutil.process_iter(attrs=["name"]):
        try:
            p.cpu_percent(None)
            name = p.info.get("name") or str(p.pid)
            processes.append((p, name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(sample_interval)

    candidates: list[tuple[float, psutil.Process, str]] = []
    for p, name in processes:
        try:
            cpu = float(p.cpu_percent(None) or 0.0)
            candidates.append((cpu, p, name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    candidates.sort(key=lambda item: item[0], reverse=True)
    procs: list[ProcStatus] = []
    for cpu, p, name in candidates[: max(memory_scan_limit, n)]:
        try:
            mem = getattr(p.memory_info(), "rss", 0) or 0
            procs.append(ProcStatus(name=name, cpu=cpu, mem=int(mem)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    procs.sort(key=lambda x: (x.cpu, x.mem), reverse=True)
    return procs[:n]


def _collect_metrics(context: Any = None) -> dict[str, Any]:
    return {
        "cpu_percent": cpu_percent(),
        "cpu_count": cpu_count(),
        "cpu_count_logical": cpu_count_logical(),
        "cpu_freq": cpu_freq(),
        "cpu_brand": get_cpu_brand(),
        "memory_stat": memory_stat(),
        "swap_stat": swap_stat(),
        "disk_usage": disk_usage(max_items=8),
        "disk_io": disk_io(),
        "network_io": network_io(),
        "process_status": process_status(),
        "time": _dt_now().strftime("%Y-%m-%d %H:%M:%S"),
        "python_version": readable_python_version(),
        "system_name": system_name(),
        "astrbot_version": resolve_astrbot_version(context),
        "bot_run_time": _format_td(_dt_now() - ASTRBOT_START_TIME),
        "system_run_time": _format_td(_dt_now() - BOOT_TIME),
    }


async def collect_all(context: Any = None) -> dict[str, Any]:
    # 同步采集放到线程池，避免 psutil/cpuinfo 阻塞 AstrBot 事件循环。
    metrics_task = asyncio.create_task(asyncio.to_thread(_collect_metrics, context))
    network_task = asyncio.create_task(connection_test())
    metrics, network_connection = await asyncio.gather(metrics_task, network_task)
    metrics["network_connection"] = network_connection
    return metrics
