"""拉起执行器（Phase 3 入场券 §7）：学习型白名单 + 防重复 + undo 分级。

护栏：
1. 学习型白名单——只拉 evidence/历史前台流里出现过的 exe 名；解析失败跳过；
2. 防重复拉起——已在运行的成员跳过（方案 Phase 3 原文要求）；
3. undo=关掉本次拉起的实例（有损；方案 undo 分级原文）。
路径解析只发生在执行瞬间（App Paths 注册表 / PATH），事件流永不记录
路径（标识中间地带红线：路径是需授权的敏感层，录制层只有 exe 名）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
PROCESS_TERMINATE = 0x0001
TH32CS_SNAPPROCESS = 0x00000002

_kernel32 = ctypes.windll.kernel32


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def running_exes() -> set[str]:
    """当前正在运行的进程 exe 名集合（小写）。"""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return set()
    out: set[str] = set()
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.add(e.szExeFile.lower())
            ok = _kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _kernel32.CloseHandle(snap)
    return out


_SHORTCUT_CACHE: dict[str, str] | None = None


def _shortcut_map() -> dict[str, str]:
    """开始菜单 .lnk 的 target 映射（exe 基名小写 → 绝对路径；懒构建缓存）。
    快捷方式名通常不带 .exe（"微信.lnk"），必须按 target 反查。"""
    global _SHORTCUT_CACHE
    if _SHORTCUT_CACHE is not None:
        return _SHORTCUT_CACHE
    mapping: dict[str, str] = {}
    try:
        import comtypes.client
        shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
        roots = [os.path.expandvars(
                     r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
                 r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"]
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    if not f.lower().endswith(".lnk"):
                        continue
                    try:
                        path = shell.CreateShortCut(
                            os.path.join(dirpath, f)).TargetPath
                        if path and os.path.isfile(path):
                            mapping.setdefault(
                                os.path.basename(path).lower(), path)
                    except Exception:
                        continue
    except Exception:
        log.debug("开始菜单快捷方式解析失败", exc_info=True)
    _SHORTCUT_CACHE = mapping
    return mapping


def resolve_path(exe: str) -> str | None:
    """exe 名 → 可执行路径：App Paths 注册表 → 开始菜单快捷方式 → PATH。"""
    if sys.platform != "win32":
        return shutil.which(exe)
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                        root, rf"Software\Microsoft\Windows\CurrentVersion"
                              rf"\App Paths\{exe}") as k:
                    path = winreg.QueryValueEx(k, "")[0]
                    if path and os.path.isfile(path.strip('"')):
                        return path.strip('"')
            except OSError:
                continue
    except Exception:
        log.debug("App Paths 解析失败 %s", exe, exc_info=True)
    path = _shortcut_map().get(exe.lower())
    if path:
        return path
    return shutil.which(exe)


def terminate_pid(pid: int) -> bool:
    """关掉自己拉起的实例（undo；有损，方案 undo 分级允许）。"""
    h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_kernel32.TerminateProcess(h, 1))
    finally:
        _kernel32.CloseHandle(h)


class FakeLaunchExecutor:
    """演练模式（rehearsal_mode）专用：记录意图、零系统副作用。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def launch(self, exes: list[str]) -> dict:
        self.calls.append(list(exes))
        return {"launched": [{"exe": e, "pid": -1} for e in exes],
                "skipped_running": [], "unresolved": [],
                "rehearsal": True}

    @staticmethod
    def undo(launched: list[dict]) -> int:
        return 0  # 无真实进程，无事可撤


class LaunchExecutor:
    """技能侧适配器：launch(exes) → 结果字典（launched 携带 pid 供 undo）。"""

    def __init__(self, whitelist: set[str] | None = None) -> None:
        # 学习型白名单：允许拉起的 exe 名集合（来自历史前台流）
        self._whitelist = whitelist

    def launch(self, exes: list[str]) -> dict:
        running = running_exes()
        result = {"launched": [], "skipped_running": [], "unresolved": []}
        for exe in exes:
            if self._whitelist is not None and exe.lower() not in self._whitelist:
                result["unresolved"].append(exe)  # 白名单外：拒绝执行
                continue
            if exe.lower() in running:
                result["skipped_running"].append(exe)
                continue
            path = resolve_path(exe)
            if path is None:
                result["unresolved"].append(exe)
                log.info("启动组合成员无法解析路径，跳过：%s", exe)
                continue
            try:
                proc = subprocess.Popen(
                    [path], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True, cwd=os.path.expanduser("~"))
                result["launched"].append({"exe": exe, "pid": proc.pid})
                log.info("拉起：%s (pid=%s)", exe, proc.pid)
            except Exception:
                result["unresolved"].append(exe)
                log.warning("拉起失败：%s", exe, exc_info=True)
        return result

    @staticmethod
    def undo(launched: list[dict]) -> int:
        """关掉本次拉起的实例；返回成功数。"""
        n = 0
        for item in launched or []:
            if terminate_pid(int(item["pid"])):
                n += 1
        return n
