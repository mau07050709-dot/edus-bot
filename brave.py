"""Detección de Brave y validación de su conexión local."""

import ctypes
import os
import re
import subprocess
import winreg
from ctypes import wintypes
from pathlib import Path


CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


def find_brave(custom_path=""):
    if custom_path:
        candidate = Path(custom_path)
        return candidate if candidate.name.lower() == "brave.exe" and candidate.is_file() else None
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if root := os.environ.get(variable):
            candidate = Path(root) / "BraveSoftware/Brave-Browser/Application/brave.exe"
            if candidate.is_file():
                return candidate
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
            ) as key:
                candidate = Path(winreg.QueryValue(key, None).strip('"'))
                if candidate.is_file():
                    return candidate
        except OSError:
            continue
    return None


def process_image(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    )
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return Path(name.value)
        return None
    finally:
        kernel.CloseHandle(handle)


def personal_debug_endpoint():
    descriptor = Path(os.environ["LOCALAPPDATA"]) / "BraveSoftware/Brave-Browser/User Data/DevToolsActivePort"
    try:
        with descriptor.open(encoding="ascii") as port_file:
            lines = port_file.read(512).splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) != 2 or not re.fullmatch(r"[0-9]{1,5}", lines[0]):
        return None
    port = int(lines[0])
    if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser/[a-zA-Z0-9-]+", lines[1]):
        return None
    return port, lines[1]


def verify_endpoint(brave, port=None):
    port = CDP_PORT if port is None else port
    netstat = Path(os.environ["SystemRoot"]) / "System32/netstat.exe"
    result = subprocess.run(
        [str(netstat), "-ano"], capture_output=True, text=True,
        errors="replace", timeout=4, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    listeners = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[0] == "TCP":
            address, remote, pid = fields[1], fields[2], fields[4]
            # Los listeners tienen puerto remoto cero, independientemente del idioma de Windows.
            if address.endswith(f":{port}") and remote.endswith(":0"):
                listeners.append((address.rsplit(":", 1)[0], int(pid)))
    local_pids = {pid for address, pid in listeners if address == "127.0.0.1"}
    if not local_pids:
        raise RuntimeError(
            "Brave no ofrece CDP local. En tu Brave personal, abre "
            "brave://inspect/#remote-debugging y habilita la depuración de esta instancia."
        )
    if any(address not in ("127.0.0.1", "[::1]") for address, _ in listeners):
        raise RuntimeError("El puerto CDP está expuesto fuera de la máquina local.")
    for pid in local_pids:
        executable = process_image(pid)
        if executable is None or not executable.samefile(brave):
            raise RuntimeError("El puerto local CDP no pertenece al Brave seleccionado.")



def resolve_endpoint(brave):
    personal = personal_debug_endpoint()
    if not personal:
        raise RuntimeError(
            "Habilita la depuración en tu Brave personal desde brave://inspect/#remote-debugging."
        )
    port, path = personal
    verify_endpoint(brave, port)
    return f"ws://127.0.0.1:{port}{path}"
