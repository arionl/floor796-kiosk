#!/usr/bin/env python3
"""
CPU affinity manager for big.LITTLE SoCs.

On the OrangePi 5 Max (RK3588), the 8 cores are split into three clusters:
  - Cluster 0 (slow/little): CPU 0-3, Cortex-A55, max 1.8 GHz
  - Cluster 1 (mid):          CPU 4-5, Cortex-A76, max 2.26 GHz
  - Cluster 2 (fast/big):     CPU 6-7, Cortex-A76, max 2.30 GHz

The main render loop is pinned to the fast cores (6-7) for maximum
single-threaded FPS.  Background threads (tile cache loader, hologram
decoder, stats HTTP server) are pinned to the slow cores (0-3) so they
never steal fast-core time slices from the render loop.

On homogeneous SoCs (e.g. Raspberry Pi 5 — 4× Cortex-A76 at 2.4 GHz),
no pinning is performed; all functions are no-ops.

Detection is based on /proc/device-tree/model (primary) and
heterogeneous max CPU frequencies (fallback).  Both methods are
read-only and require no special permissions — sched_setaffinity(2)
on the calling thread (pid=0) works as an unprivileged user.
"""

import logging
import os

log = logging.getLogger("floor796")

# Cache the detection result so we only read sysfs once.
_topology = None


def _read_cpu_max_freq(cpu_id):
    """Read max frequency (in kHz) for a given CPU, or 0 on error."""
    path = f"/sys/devices/system/cpu/cpu{cpu_id}/cpufreq/cpuinfo_max_freq"
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _read_device_model():
    """Read the device model string from /proc/device-tree/model."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return f.read().rstrip(b"\x00").decode("ascii", errors="replace")
    except OSError:
        return ""


def detect_big_little():
    """Detect big.LITTLE CPU topology.

    Returns (fast_cores, slow_cores) tuples:
      - fast_cores: set of CPU IDs for the highest-frequency cluster
      - slow_cores: set of CPU IDs for the lowest-frequency cluster

    Returns (None, None) if the SoC is homogeneous (all cores same freq)
    or if detection fails — in which case pinning is skipped.
    """
    global _topology
    if _topology is not None:
        return _topology

    n_cpus = os.cpu_count() or 1
    freqs = {}
    for i in range(n_cpus):
        freqs[i] = _read_cpu_max_freq(i)

    # Filter out any CPUs with 0 freq (failed reads)
    valid_freqs = {c: f for c, f in freqs.items() if f > 0}
    if not valid_freqs:
        _topology = (None, None)
        return _topology

    max_freq = max(valid_freqs.values())
    min_freq = min(valid_freqs.values())

    # If all cores have the same max frequency, it's homogeneous.
    if max_freq == min_freq:
        _topology = (None, None)
        return _topology

    fast_cores = set(c for c, f in valid_freqs.items() if f == max_freq)
    slow_cores = set(c for c, f in valid_freqs.items() if f == min_freq)

    model = _read_device_model()
    log.info("CPU topology: big.LITTLE detected (%s)", model or "unknown SoC")
    log.info("  Fast cores: %s (%d MHz)",
             sorted(fast_cores), max_freq // 1000)
    log.info("  Slow cores: %s (%d MHz)",
             sorted(slow_cores), min_freq // 1000)

    _topology = (fast_cores, slow_cores)
    return _topology


def pin_main_thread():
    """Pin the calling (main/render) thread to the fast cores.

    No-op on homogeneous SoCs like the Raspberry Pi 5.
    """
    fast, _ = detect_big_little()
    if fast is None:
        return False
    try:
        os.sched_setaffinity(0, fast)
        log.info("Main thread pinned to fast cores: %s", sorted(fast))
        return True
    except OSError as e:
        log.warning("Could not pin main thread: %s", e)
        return False


def pin_background_thread(name="background"):
    """Pin the calling thread to the slow cores.

    Call this at the start of a background thread's loop.
    No-op on homogeneous SoCs like the Raspberry Pi 5.
    """
    _, slow = detect_big_little()
    if slow is None:
        return False
    try:
        os.sched_setaffinity(0, slow)
        log.debug("Thread '%s' pinned to slow cores: %s", name, sorted(slow))
        return True
    except OSError as e:
        log.warning("Could not pin thread '%s': %s", name, e)
        return False


def get_affinity_info():
    """Return a dict with topology info for stats/logging."""
    fast, slow = detect_big_little()
    if fast is None:
        return {"big_little": False}
    return {
        "big_little": True,
        "fast_cores": sorted(fast),
        "slow_cores": sorted(slow),
    }
