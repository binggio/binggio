# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ashish Kumar
"""How much Binggio does at once.

Profiling runs one table per thread: its work is SQLite queries over the prepared copies, which run
outside Python's interpreter lock, so threads use several cores. Preparing tables (reading CSV
files) is Python work, so it runs in separate processes when the platform allows it, and falls
back to threads otherwise.

    BINGGIO_WORKERS=4      tables at once (default: the number of processor cores, at most 16)
    BINGGIO_WORKERS=1      one at a time, as before 0.1.9
    BINGGIO_PROCESSES=0    prepare tables in threads instead of processes
"""
from __future__ import annotations
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def workers(cap=16):
    env = (os.environ.get('BINGGIO_WORKERS') or '').strip()
    if env.isdigit() and int(env) > 0: return min(int(env), 64)
    return max(1, min(cap, os.cpu_count() or 2))


def processes_allowed():
    return (os.environ.get('BINGGIO_PROCESSES') or '1').strip() not in ('0', 'no', 'off', 'false')


def run(items, fn, progress=None, n_workers=None, label=lambda x: str(x)):
    """fn(item) for every item, `n_workers` at a time. Returns [(item, result, error)] in input order.
    progress(done_count, total, label) is called as each item finishes."""
    items = list(items)
    n = n_workers or workers()
    out = [None] * len(items)
    lock = threading.Lock(); done = [0]

    def finished(i, res, err):
        out[i] = (items[i], res, err)
        if progress:
            with lock:
                done[0] += 1
                progress(done[0], len(items), label(items[i]))

    if n <= 1 or len(items) <= 1:
        for i, it in enumerate(items):
            try: finished(i, fn(it), None)
            except Exception as e: finished(i, None, e)
        return out
    with ThreadPoolExecutor(max_workers=min(n, len(items)), thread_name_prefix='binggio') as pool:
        futs = {pool.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            i = futs[f]
            try: finished(i, f.result(), None)
            except Exception as e: finished(i, None, e)
    return out
