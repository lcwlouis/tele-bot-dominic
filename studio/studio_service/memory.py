"""Sample host availability and physical footprint of studio process groups."""

import asyncio
import ctypes
from dataclasses import dataclass
import os
import re
import subprocess
import sys


@dataclass(frozen=True)
class Reading:
    available: int
    footprint: int


def parse_vm_stat(output: str) -> int:
    page_size = re.search(r"page size of ([0-9]+) bytes", output)
    pages = dict(re.findall(r"(?m)^(Pages free|Pages inactive|Pages purgeable):\s+(\d+)\.$", output))
    if page_size is None or len(pages) != 3 or int(page_size[1]) == 0:
        raise ValueError("incomplete vm_stat output")
    return int(page_size[1]) * sum(map(int, pages.values()))


class DarwinMemory:
    def __init__(self):
        if sys.platform != "darwin":
            raise RuntimeError("studio memory telemetry requires macOS")
        self.usage = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage
        self.usage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

    def _sample(self, groups):
        available = parse_vm_stat(subprocess.check_output(
            ["/usr/bin/vm_stat"], text=True, timeout=1
        ))
        processes = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,pgid="], text=True, timeout=1
        )
        footprint = 0
        for line in processes.splitlines():
            pid, group = map(int, line.split())
            if group in groups or pid == os.getpid():
                # RUSAGE_INFO_V0 places ri_phys_footprint after the UUID and seven counters.
                info = (ctypes.c_uint64 * 12)()
                if self.usage(pid, 0, ctypes.byref(info)) == 0:
                    footprint += info[9]
        return Reading(available, footprint)

    async def __call__(self, groups):
        return await asyncio.to_thread(self._sample, groups)
