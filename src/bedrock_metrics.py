"""Fast Windows process/memory sampling with the native API (no shell process)."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w


def windows_snapshot() -> dict:
    kernel = c.WinDLL('kernel32', use_last_error=True)
    psapi = c.WinDLL('psapi', use_last_error=True)
    size_t = c.c_size_t

    class ProcessEntry(c.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('name', w.WCHAR * 260)]

    class Memory(c.Structure):
        _fields_ = [('size', w.DWORD), ('load', w.DWORD)] + [
            (name, c.c_ulonglong) for name in
            ('total', 'free', 'totalpage', 'freepage', 'totalvirtual', 'freevirtual', 'extended')]

    class Counters(c.Structure):
        _fields_ = [('size', w.DWORD), ('faults', w.DWORD)] + [
            (name, size_t) for name in
            ('peak', 'working', 'peakpaged', 'paged', 'peaknonpaged', 'nonpaged', 'page', 'peakpage')]

    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GlobalMemoryStatusEx.argtypes = [c.POINTER(Memory)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Counters), w.DWORD]
    memory = Memory(size=c.sizeof(Memory))
    if not kernel.GlobalMemoryStatusEx(c.byref(memory)):
        raise c.WinError(c.get_last_error())
    result = dict(found=False, pids=[], ws=0, cpu=0.0, total=memory.total, free=memory.free)
    snap = kernel.CreateToolhelp32Snapshot(2, 0)
    if snap == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        entry = ProcessEntry(size=c.sizeof(ProcessEntry))
        more = kernel.Process32FirstW(snap, c.byref(entry))
        while more:
            if entry.name.casefold() == 'bedrock_server.exe':
                result['found'] = True
                result['pids'].append(entry.pid)
                handle = kernel.OpenProcess(0x0400 | 0x0010, False, entry.pid)
                if handle:
                    try:
                        counters = Counters(size=c.sizeof(Counters))
                        if psapi.GetProcessMemoryInfo(handle, c.byref(counters), c.sizeof(counters)):
                            result['ws'] += counters.working
                        times = [w.FILETIME() for _ in range(4)]
                        if kernel.GetProcessTimes(handle, *(c.byref(t) for t in times)):
                            result['cpu'] += sum((t.dwHighDateTime << 32) + t.dwLowDateTime
                                                 for t in times[2:]) / 10000000
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snap, c.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    return result
