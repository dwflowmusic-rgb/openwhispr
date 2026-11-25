#!/usr/bin/env python3
"""Auto-patcher idempotente para manter o Auto-Enter no OpenWhispr apos atualizacoes."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import msvcrt
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PATCH_VERSION = "V1"
MARKER = "OPENWHISPR_AUTO_ENTER_PATCH_V1"
UPDATER_MARKER = "OPENWHISPR_AUTO_ENTER_UPDATER_HOOK_V1"

BASE_DIR = Path(r"C:\Users\giand\.openwhispr\auto-enter-patch")
PATCH_BIN_SRC = BASE_DIR / "bin" / "windows-press-enter.exe"
BACKUPS_DIR = BASE_DIR / "backups"
STAMP_FILE = BASE_DIR / ".patched-stamp.json"
LOCK_FILE = BASE_DIR / "patch.lock"
LOG_FILE = BASE_DIR / "patch.log"

INSTALL_DIR = Path(r"C:\Program Files\OpenWhispr")
RESOURCES_DIR = INSTALL_DIR / "resources"
APP_ASAR = RESOURCES_DIR / "app.asar"
TARGET_BIN = RESOURCES_DIR / "bin" / "windows-press-enter.exe"
OPENWHISPR_EXE = INSTALL_DIR / "OpenWhispr.exe"
PYTHONW_EXE = Path(r"C:\Users\giand\AppData\Local\Programs\Python\Python312\pythonw.exe")

BLOCK_SIZE = 4 * 1024 * 1024  # 4 MiB default do @electron/asar


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def acquire_lock():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    f = LOCK_FILE.open("a+", encoding="utf-8")
    for _ in range(60):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Timeout aguardando lock de patch_openwhispr.py")


def release_lock(f) -> None:
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        f.close()
    except OSError:
        pass


def running_processes(names: list[str]) -> list[str]:
    found: list[str] = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore")
        low_targets = [n.lower() for n in names]
        for line in out.splitlines():
            if not line.strip():
                continue
            proc_name = line.split(",")[0].strip('"').lower()
            for t in low_targets:
                if t in proc_name and proc_name not in found:
                    found.append(proc_name)
    except Exception:
        pass
    return found


def wait_for_updater_exit(timeout_s: int = 90) -> bool:
    targets = ["openwhispr-setup", "installer.exe"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        procs = running_processes(targets)
        if not procs:
            return True
        time.sleep(1.0)
    return False


def stop_openwhispr() -> bool:
    was_running = bool(running_processes(["openwhispr.exe"]))
    if not was_running:
        return False
    log("Encerrando OpenWhispr.exe para atualizar app.asar...")
    subprocess.run(
        ["taskkill", "/IM", "OpenWhispr.exe", "/T"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not running_processes(["openwhispr.exe"]):
            return True
        time.sleep(0.3)
    subprocess.run(
        ["taskkill", "/F", "/IM", "OpenWhispr.exe", "/T"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    return True


def get_thread_desktop_name() -> str:
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hd = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not hd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_uint32()
        if user32.GetUserObjectInformationW(hd, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return buf.value
    except Exception:
        pass
    return ""


def start_openwhispr_non_elevated() -> None:
    if not OPENWHISPR_EXE.exists():
        return
    desktop = get_thread_desktop_name()
    if desktop.lower() == "default":
        # explorer.exe sempre inicia o processo filho no token de usuario comum do desktop Default.
        subprocess.Popen(
            ["explorer.exe", str(OPENWHISPR_EXE)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("OpenWhispr.exe iniciado via explorer.exe (desktop Default).")
        return

    # Quando chamado de um desktop isolado/oculto (ex.: exebox-*), delega ao
    # Agendador de Tarefas (svchost Schedule) para abrir em WinSta0\Default.
    task_name = "OpenWhispr-Relaunch-Interactive"
    ps_script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$t = '{task_name}'; "
        f"$a = New-ScheduledTaskAction -Execute '{OPENWHISPR_EXE}'; "
        "$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
        "-LogonType Interactive -RunLevel Limited; "
        "Register-ScheduledTask -TaskName $t -Action $a -Principal $p -Force | Out-Null; "
        "try { Start-ScheduledTask -TaskName $t; Start-Sleep -Seconds 2 } "
        "finally { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }"
    )
    res = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps_script,
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Falha ao iniciar OpenWhispr no desktop Default (desktop={desktop!r}): {res.stderr.strip()}"
        )
    log(f"OpenWhispr.exe iniciado no desktop Default via Schedule (origem={desktop or 'unknown'}).")


def ensure_acl_and_bin() -> None:
    if is_admin():
        user = os.environ.get("USERNAME", "giand")
        subprocess.run(
            ["icacls", str(INSTALL_DIR), "/grant", f"{user}:(OI)(CI)M", "/Q"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if PATCH_BIN_SRC.exists():
        TARGET_BIN.parent.mkdir(parents=True, exist_ok=True)
        need_copy = not TARGET_BIN.exists() or TARGET_BIN.read_bytes() != PATCH_BIN_SRC.read_bytes()
        if need_copy:
            shutil.copy2(PATCH_BIN_SRC, TARGET_BIN)
            log(f"Binario copiado para {TARGET_BIN}")


class AsarArchive:
    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as f:
            p1, header_size, p2, json_size = struct.unpack("<IIII", f.read(16))
            self.data_offset = 8 + header_size
            self.header = json.loads(f.read(json_size).decode("utf-8"))

    def list_files(self) -> dict[str, dict]:
        out: dict[str, dict] = {}

        def walk(node: dict, prefix: str = "") -> None:
            for name, info in node.get("files", {}).items():
                rel = f"{prefix}/{name}" if prefix else name
                if "files" in info:
                    walk(info, rel)
                else:
                    out[rel] = info

        walk(self.header)
        return out

    def find_single(self, pattern: str) -> str:
        rx = re.compile(pattern)
        matches = [p for p in self.list_files() if rx.fullmatch(p)]
        if len(matches) != 1:
            raise RuntimeError(f"Esperado exatamente 1 arquivo para {pattern}, encontrados {len(matches)}: {matches}")
        return matches[0]

    def read_bytes(self, rel_path: str) -> bytes:
        node = self.header
        for part in rel_path.split("/"):
            node = node["files"][part]
        if node.get("unpacked"):
            return (self.path.parent / "app.asar.unpacked" / rel_path).read_bytes()
        off = int(node["offset"])
        sz = int(node["size"])
        with self.path.open("rb") as f:
            f.seek(self.data_offset + off)
            return f.read(sz)

    def write_patched(self, replacements: dict[str, bytes], dest_tmp: Path) -> None:
        new_header = json.loads(json.dumps(self.header))
        packed_entries: list[tuple[str, dict, bytes]] = []
        current_offset = 0

        def walk_build(orig_node: dict, new_node: dict, prefix: str = "") -> None:
            nonlocal current_offset
            for name, info in orig_node.get("files", {}).items():
                rel = f"{prefix}/{name}" if prefix else name
                n_info = new_node["files"][name]
                if "files" in info:
                    walk_build(info, n_info, rel)
                elif info.get("unpacked"):
                    continue
                else:
                    if rel in replacements:
                        data = replacements[rel]
                        blocks = [
                            hashlib.sha256(data[i : i + BLOCK_SIZE]).hexdigest()
                            for i in range(0, len(data), BLOCK_SIZE)
                        ] or [hashlib.sha256(b"").hexdigest()]
                        n_info["size"] = len(data)
                        n_info["integrity"] = {
                            "algorithm": "SHA256",
                            "hash": hashlib.sha256(data).hexdigest(),
                            "blockSize": BLOCK_SIZE,
                            "blocks": blocks,
                        }
                    else:
                        data = self.read_bytes(rel)
                    n_info["offset"] = str(current_offset)
                    current_offset += len(data)
                    packed_entries.append((rel, n_info, data))

        walk_build(self.header, new_header)

        json_bytes = json.dumps(new_header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        json_size = len(json_bytes)
        padded_size = (json_size + 3) & ~3
        padding = b"\x00" * (padded_size - json_size)
        p2 = 4 + padded_size
        header_size = 4 + p2
        preamble = struct.pack("<IIII", 4, header_size, p2, json_size)

        with dest_tmp.open("wb") as out:
            out.write(preamble)
            out.write(json_bytes)
            out.write(padding)
            for _, _, data in packed_entries:
                out.write(data)

        validate_asar(dest_tmp, current_offset)


def validate_asar(asar_path: Path, expected_payload_size: int | None = None) -> dict:
    total_size = asar_path.stat().st_size
    with asar_path.open("rb") as f:
        p1, header_size, p2, json_size = struct.unpack("<IIII", f.read(16))
        if p1 != 4 or p2 != header_size - 4:
            raise RuntimeError("Pickle header ASAR invalido")
        data_offset = 8 + header_size
        header = json.loads(f.read(json_size).decode("utf-8"))

    max_end = 0

    def walk_check(node: dict) -> None:
        nonlocal max_end
        for info in node.get("files", {}).values():
            if "files" in info:
                walk_check(info)
            elif not info.get("unpacked"):
                end = int(info["offset"]) + int(info["size"])
                if end > max_end:
                    max_end = end

    walk_check(header)
    if expected_payload_size is not None and max_end != expected_payload_size:
        raise RuntimeError(f"Payload ASAR inconsistente: {max_end} != {expected_payload_size}")
    if data_offset + max_end != total_size:
        raise RuntimeError(f"Tamanho ASAR inconsistente: {data_offset + max_end} != {total_size}")
    return header


def sub_once(pattern: str, repl, text: str, label: str, flags: int = 0) -> str:
    new_text, count = re.subn(pattern, repl, text, count=0, flags=flags)
    if count != 1:
        raise RuntimeError(f"[{label}] Esperada 1 ocorrencia de {pattern!r}, obtidas {count}")
    return new_text


def patch_settings_store(text: str) -> str:
    if "autoEnterEnabled" in text:
        return text
    text = sub_once(
        r"\.autoPasteEnabled\.",
        ".autoPasteEnabled.autoEnterEnabled.",
        text,
        "settingsStore.BOOLEAN_SETTINGS",
    )
    text = sub_once(
        r"autoPasteEnabled:([A-Za-z_$]+)\(`autoPasteEnabled`,!0\)",
        r"autoPasteEnabled:\1(`autoPasteEnabled`,!0),autoEnterEnabled:\1(`autoEnterEnabled`,!1)",
        text,
        "settingsStore.init",
    )
    text = sub_once(
        r"setAutoPasteEnabled:([A-Za-z_$]+)\(`autoPasteEnabled`\)",
        r"setAutoPasteEnabled:\1(`autoPasteEnabled`),setAutoEnterEnabled:\1(`autoEnterEnabled`)",
        text,
        "settingsStore.setter",
    )
    return text


def patch_index_bundle(text: str) -> str:
    if "__owAutoEnter" in text:
        return text
    text, expose_count = re.subn(
        r"autoPasteEnabled:([A-Za-z_$]+)\.autoPasteEnabled,setAutoPasteEnabled:\1\.setAutoPasteEnabled",
        r"autoPasteEnabled:\1.autoPasteEnabled,setAutoPasteEnabled:\1.setAutoPasteEnabled,autoEnterEnabled:\1.autoEnterEnabled,setAutoEnterEnabled:\1.setAutoEnterEnabled",
        text,
    )
    if expose_count < 1:
        raise RuntimeError("[index.js] Padrao useSettingsInternal autoPasteEnabled nao encontrado")

    text = sub_once(
        r"\{autoPasteEnabled:([A-Za-z_$]+),keepTranscriptionInClipboard:([A-Za-z_$]+)\}=([A-Za-z_$]+)\(\)",
        r"{autoPasteEnabled:\1,keepTranscriptionInClipboard:\2,autoEnterEnabled:__owAutoEnter}=\3()",
        text,
        "index.js.getSettings",
    )
    text = sub_once(
        r"safePaste\(([A-Za-z_$]+)\.text,\{\.\.\.([A-Za-z_$]+)\?\{fromStreaming:!0\}:\{\},\.\.\.([A-Za-z_$]+)\}\)",
        r"safePaste(\1.text,{...\2?{fromStreaming:!0}:{},...\3,...__owAutoEnter?{pressEnter:!0}:{}})",
        text,
        "index.js.safePaste",
    )
    return text


def patch_settings_modal(text: str) -> str:
    if "__owAeVal" in text:
        return text
    text = sub_once(
        r"autoPasteEnabled:([A-Za-z_$]+),setAutoPasteEnabled:([A-Za-z_$]+),keepTranscriptionInClipboard:",
        r"autoPasteEnabled:\1,setAutoPasteEnabled:\2,autoEnterEnabled:__owAeVal,setAutoEnterEnabled:__owAeSet,keepTranscriptionInClipboard:",
        text,
        "SettingsModal.destructure",
    )

    def add_toggle(m: re.Match[str]) -> str:
        ns, panel_row, row, t_fn, toggle, ap_var, _ap_set = m.groups()
        row_jsx = (
            f',(0,{ns}.jsx)({panel_row},{{children:(0,{ns}.jsx)({row},{{'
            f'label:{t_fn}(`settingsPage.general.clipboard.autoEnter`,{{defaultValue:`Pressionar Enter após colar`}}),'
            f'description:{t_fn}(`settingsPage.general.clipboard.autoEnterDescription`,{{defaultValue:`Pressionar Enter automaticamente após colar a transcrição para enviar mensagens ou comandos`}}),'
            f'children:(0,{ns}.jsx)({toggle},{{checked:__owAeVal,onChange:__owAeSet,disabled:!{ap_var}}})}})}})'
        )
        return m.group(0) + row_jsx

    text = sub_once(
        r"\(0,([A-Za-z_$]+)\.jsx\)\(([A-Za-z_$]+),\{children:\(0,\1\.jsx\)\(([A-Za-z_$]+),\{label:([A-Za-z_$]+)\([`'\"]settingsPage\.general\.clipboard\.autoPaste[`'\"]\),description:\4\([`'\"]settingsPage\.general\.clipboard\.autoPasteDescription[`'\"]\),children:\(0,\1\.jsx\)\(([A-Za-z_$]+),\{checked:([A-Za-z_$]+),onChange:([A-Za-z_$]+)\}\)\}\)\}\)",
        add_toggle,
        text,
        "SettingsModal.toggleRow",
    )
    return text


def patch_i18n_bundle(text: str) -> str:
    if '"autoEnter":' in text:
        return text
    text = sub_once(
        r'"autoPaste":"Colagem automática","autoPasteDescription":"Colar automaticamente o texto transcrito no aplicativo ativo ao finalizar o ditado"',
        r'\g<0>,"autoEnter":"Pressionar Enter após colar","autoEnterDescription":"Pressionar Enter automaticamente após colar a transcrição para enviar mensagens ou comandos"',
        text,
        "i18n.pt",
    )
    text = sub_once(
        r'"autoPaste":"Automatic pasting","autoPasteDescription":"Automatically paste transcribed text into the active app when dictation finishes"',
        r'\g<0>,"autoEnter":"Press Enter after pasting","autoEnterDescription":"Automatically press Enter once the transcription is pasted to send messages or submit commands"',
        text,
        "i18n.en",
    )
    return text


def patch_ipc_handlers(text: str) -> str:
    if "options?.pressEnter === true" in text or "pasteOptions?.pressEnter === true" in text:
        return text
    if re.search(r"const textToPaste = raw === true \? text : applySmartSpacing\(text\);", text):
        return sub_once(
            r"const textToPaste = raw === true \? text : applySmartSpacing\(text\);",
            "const textToPaste = raw === true || options?.pressEnter === true ? text : applySmartSpacing(text);",
            text,
            "ipcHandlers.smartSpacing",
        )
    return sub_once(
        r"const textToPaste = applySmartSpacing\(text\);",
        "const textToPaste = options?.pressEnter === true ? text : applySmartSpacing(text);",
        text,
        "ipcHandlers.smartSpacingFallback",
    )


def patch_clipboard(text: str) -> str:
    if MARKER in text:
        return text
    text = f"/* {MARKER} */\n" + text

    resolve_press_enter = (
        "\r\n\r\n  resolveWindowsPressEnterBinary() {\r\n"
        '    return this._resolveNativeBinary(\r\n'
        '      "windows-press-enter.exe",\r\n'
        '      "win32",\r\n'
        '      "winPressEnterChecked",\r\n'
        '      "winPressEnterPath"\r\n'
        "    );\r\n"
        "  }"
    )
    text = sub_once(
        r'resolveWindowsFastPasteBinary\(\) \{\s*return this\._resolveNativeBinary\(\s*"windows-fast-paste\.exe",\s*"win32",\s*"winFastPasteChecked",\s*"winFastPastePath"\s*\);\s*\}',
        lambda m: m.group(0) + resolve_press_enter,
        text,
        "clipboard.resolveWindowsPressEnterBinary",
    )

    paste_replacement = (
        "  async pasteText(text, options = {}) {\r\n"
        "    return this._runClipboardOperation(\r\n"
        "      async () => {\r\n"
        "        const result = await this._pasteText(text, options);\r\n"
        "        if (options.pressEnter === true && result?.pasted !== false) {\r\n"
        "          await this.pressEnterAfterPaste();\r\n"
        "        }\r\n"
        "        return result;\r\n"
        "      },\r\n"
        "      (result) => result?.restoreComplete\r\n"
        "    );\r\n"
        "  }\r\n\r\n"
        "  async pressEnterAfterPaste() {\r\n"
        '    if (process.platform !== "win32") return false;\r\n'
        "    await new Promise((resolve) => setTimeout(resolve, 120));\r\n"
        "    const homeFallback = path.join(\r\n"
        "      os.homedir(),\r\n"
        '      ".openwhispr",\r\n'
        '      "auto-enter-patch",\r\n'
        '      "bin",\r\n'
        '      "windows-press-enter.exe"\r\n'
        "    );\r\n"
        "    const nativeBin =\r\n"
        "      this.resolveWindowsPressEnterBinary() ||\r\n"
        "      (fs.existsSync(homeFallback) ? homeFallback : null);\r\n"
        "    if (nativeBin) {\r\n"
        "      const ok = await new Promise((resolve) => {\r\n"
        "        let done = false;\r\n"
        "        const proc = spawn(nativeBin, [], {\r\n"
        '          stdio: ["ignore", "pipe", "pipe"],\r\n'
        "          windowsHide: true,\r\n"
        "        });\r\n"
        "        const timer = setTimeout(() => {\r\n"
        "          if (done) return;\r\n"
        "          done = true;\r\n"
        '          killProcess(proc, "SIGKILL");\r\n'
        "          resolve(false);\r\n"
        "        }, 2000);\r\n"
        '        proc.on("close", (code) => {\r\n'
        "          if (done) return;\r\n"
        "          done = true;\r\n"
        "          clearTimeout(timer);\r\n"
        "          resolve(code === 0);\r\n"
        "        });\r\n"
        '        proc.on("error", () => {\r\n'
        "          if (done) return;\r\n"
        "          done = true;\r\n"
        "          clearTimeout(timer);\r\n"
        "          resolve(false);\r\n"
        "        });\r\n"
        "      });\r\n"
        "      if (ok) {\r\n"
        '        this.safeLog("⏎ Auto-Enter pressed via native binary");\r\n'
        "        return true;\r\n"
        "      }\r\n"
        "    }\r\n"
        "    return new Promise((resolve) => {\r\n"
        "      let done = false;\r\n"
        "      const ps = spawn(\r\n"
        '        "powershell.exe",\r\n'
        "        [\r\n"
        '          "-NoProfile",\r\n'
        '          "-NonInteractive",\r\n'
        '          "-WindowStyle",\r\n'
        '          "Hidden",\r\n'
        '          "-ExecutionPolicy",\r\n'
        '          "Bypass",\r\n'
        '          "-Command",\r\n'
        "          \"[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')\",\r\n"
        "        ],\r\n"
        '        { stdio: ["ignore", "ignore", "pipe"], windowsHide: true }\r\n'
        "      );\r\n"
        "      const timer = setTimeout(() => {\r\n"
        "        if (done) return;\r\n"
        "        done = true;\r\n"
        '        killProcess(ps, "SIGKILL");\r\n'
        "        resolve(false);\r\n"
        "      }, 4000);\r\n"
        '      ps.on("close", (code) => {\r\n'
        "        if (done) return;\r\n"
        "        done = true;\r\n"
        "        clearTimeout(timer);\r\n"
        '        if (code === 0) this.safeLog("⏎ Auto-Enter pressed via PowerShell");\r\n'
        '        else debugLogger.warn("Auto-Enter PowerShell failed", { code }, "clipboard");\r\n'
        "        resolve(code === 0);\r\n"
        "      });\r\n"
        '      ps.on("error", (err) => {\r\n'
        "        if (done) return;\r\n"
        "        done = true;\r\n"
        "        clearTimeout(timer);\r\n"
        '        debugLogger.warn("Auto-Enter PowerShell error", { error: err?.message }, "clipboard");\r\n'
        "        resolve(false);\r\n"
        "      });\r\n"
        "    });\r\n"
        "  }"
    )

    text = sub_once(
        r"  async pasteText\(text, options = \{\}\) \{\s*return this\._runClipboardOperation\(\s*\(\) => this\._pasteText\(text, options\),\s*\(result\) => result\?\.restoreComplete\s*\);\s*\}",
        lambda _: paste_replacement,
        text,
        "clipboard.pasteText",
    )
    return text


def patch_updater(text: str) -> str:
    if UPDATER_MARKER in text:
        return text
    text = f"/* {UPDATER_MARKER} */\n" + text

    helper_method = (
        "\r\n\r\n  _spawnAutoEnterPostUpdateWatcher() {\r\n"
        '    if (process.platform !== "win32" || this._autoEnterWatcherSpawned) return;\r\n'
        "    try {\r\n"
        '      const fs = require("fs");\r\n'
        '      const path = require("path");\r\n'
        '      const os = require("os");\r\n'
        '      const { spawn } = require("child_process");\r\n'
        '      const scriptPath = path.join(os.homedir(), ".openwhispr", "auto-enter-patch", "patch_openwhispr.py");\r\n'
        "      if (!fs.existsSync(scriptPath)) return;\r\n"
        f'      const preferredPythonW = {json.dumps(str(PYTHONW_EXE))};\r\n'
        '      const pythonExe = fs.existsSync(preferredPythonW) ? preferredPythonW : "pythonw.exe";\r\n'
        "      this._autoEnterWatcherSpawned = true;\r\n"
        '      const child = spawn(pythonExe, [scriptPath, "--wait-update"], {\r\n'
        "        detached: true,\r\n"
        '        stdio: "ignore",\r\n'
        "        windowsHide: true,\r\n"
        "      });\r\n"
        "      child.unref();\r\n"
        "    } catch (error) {\r\n"
        '      console.warn("Auto-Enter post-update watcher spawn failed:", error?.message);\r\n'
        "    }\r\n"
        "  }"
    )

    text = sub_once(
        r"  _quitAndInstall\(\) \{",
        lambda m: helper_method.lstrip("\r\n") + "\r\n\r\n" + m.group(0),
        text,
        "updater.addHelper",
    )

    text = sub_once(
        r'(this\.isInstalling = true;\s*console\.log\("🔄 Installing update and restarting\.\.\."\);\s*try \{)',
        r"\1\r\n      this._spawnAutoEnterPostUpdateWatcher();",
        text,
        "updater.quitAndInstallHook",
    )

    text = sub_once(
        r"this\.handleBeforeQuit = \(\) => this\._syncInstallOnQuit\(\{ quitting: true \}\);",
        (
            "this.handleBeforeQuit = () => {\r\n"
            "        this._syncInstallOnQuit({ quitting: true });\r\n"
            "        if (this.updateDownloaded && !this.installDeferred) {\r\n"
            "          this._spawnAutoEnterPostUpdateWatcher();\r\n"
            "        }\r\n"
            "      };"
        ),
        text,
        "updater.beforeQuitHook",
    )
    return text


def patch_optional_sources(archive: AsarArchive, replacements: dict[str, bytes]) -> None:
    files = archive.list_files()
    if "src/hooks/useSettings.ts" in files:
        us = archive.read_bytes("src/hooks/useSettings.ts").decode("utf-8")
        if "autoEnterEnabled:" not in us:
            m = re.search(
                r"autoPasteEnabled: store\.autoPasteEnabled,\r?\n\s*setAutoPasteEnabled: store\.setAutoPasteEnabled,",
                us,
            )
            if m:
                us = us[: m.end()] + "\r\n    autoEnterEnabled: store.autoEnterEnabled,\r\n    setAutoEnterEnabled: store.setAutoEnterEnabled," + us[m.end() :]
                replacements["src/hooks/useSettings.ts"] = us.encode("utf-8")

    if "src/hooks/useAudioRecording.js" in files:
        uar = archive.read_bytes("src/hooks/useAudioRecording.js").decode("utf-8")
        if "autoEnterEnabled" not in uar:
            uar2 = uar.replace(
                "const { autoPasteEnabled, keepTranscriptionInClipboard } = getSettings();",
                "const { autoPasteEnabled, keepTranscriptionInClipboard, autoEnterEnabled } = getSettings();",
            )
            uar2 = uar2.replace(
                "                ...pasteOptions,\r\n              })",
                "                ...pasteOptions,\r\n                ...(autoEnterEnabled ? { pressEnter: true } : {}),\r\n              })",
            )
            if uar2 != uar:
                replacements["src/hooks/useAudioRecording.js"] = uar2.encode("utf-8")


def write_stamp() -> None:
    st = APP_ASAR.stat()
    STAMP_FILE.write_text(
        json.dumps(
            {
                "asar_size": st.st_size,
                "asar_mtime_ns": st.st_mtime_ns,
                "patch_version": PATCH_VERSION,
                "updated_at": datetime.now().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def stamp_matches() -> bool:
    if not STAMP_FILE.exists() or not APP_ASAR.exists() or not TARGET_BIN.exists():
        return False
    try:
        data = json.loads(STAMP_FILE.read_text(encoding="utf-8"))
        st = APP_ASAR.stat()
        return (
            data.get("patch_version") == PATCH_VERSION
            and data.get("asar_size") == st.st_size
            and data.get("asar_mtime_ns") == st.st_mtime_ns
        )
    except Exception:
        return False


def is_asar_patched(archive: AsarArchive) -> bool:
    cb = archive.read_bytes("src/helpers/clipboard.js").decode("utf-8", errors="ignore")
    upd = archive.read_bytes("src/updater.js").decode("utf-8", errors="ignore")
    return MARKER in cb and UPDATER_MARKER in upd


def apply_patch(restart_mode: str = "if_running", force: bool = False) -> bool:
    if not APP_ASAR.exists():
        raise FileNotFoundError(f"Nao encontrado: {APP_ASAR}")

    ensure_acl_and_bin()
    archive = AsarArchive(APP_ASAR)
    if not force and is_asar_patched(archive):
        write_stamp()
        log("app.asar ja esta patcheado; carimbo atualizado.")
        return False

    pkg = json.loads(archive.read_bytes("package.json").decode("utf-8"))
    version = pkg.get("version", "unknown")
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUPS_DIR / f"app.asar.{version}.bak"
    if not is_asar_patched(archive) and not backup_path.exists():
        shutil.copy2(APP_ASAR, backup_path)
        log(f"Backup criado em {backup_path}")

    settings_store_path = archive.find_single(r"src/dist/assets/settingsStore-[^/]+\.js")
    index_bundle_path = archive.find_single(r"src/dist/assets/index-[^/]+\.js")
    settings_modal_path = archive.find_single(r"src/dist/assets/SettingsModal-[^/]+\.js")
    i18n_bundle_path = archive.find_single(r"src/dist/assets/i18n-[^/]+\.js")

    replacements: dict[str, bytes] = {
        settings_store_path: patch_settings_store(
            archive.read_bytes(settings_store_path).decode("utf-8")
        ).encode("utf-8"),
        index_bundle_path: patch_index_bundle(
            archive.read_bytes(index_bundle_path).decode("utf-8")
        ).encode("utf-8"),
        settings_modal_path: patch_settings_modal(
            archive.read_bytes(settings_modal_path).decode("utf-8")
        ).encode("utf-8"),
        i18n_bundle_path: patch_i18n_bundle(
            archive.read_bytes(i18n_bundle_path).decode("utf-8")
        ).encode("utf-8"),
        "src/helpers/ipcHandlers.js": patch_ipc_handlers(
            archive.read_bytes("src/helpers/ipcHandlers.js").decode("utf-8")
        ).encode("utf-8"),
        "src/helpers/clipboard.js": patch_clipboard(
            archive.read_bytes("src/helpers/clipboard.js").decode("utf-8")
        ).encode("utf-8"),
        "src/updater.js": patch_updater(
            archive.read_bytes("src/updater.js").decode("utf-8")
        ).encode("utf-8"),
    }
    patch_optional_sources(archive, replacements)

    tmp_asar = RESOURCES_DIR / "app.asar.tmp"
    if tmp_asar.exists():
        tmp_asar.unlink()
    archive.write_patched(replacements, tmp_asar)

    was_running = stop_openwhispr()
    os.replace(tmp_asar, APP_ASAR)
    write_stamp()
    log(f"Patch {PATCH_VERSION} aplicado com sucesso ao OpenWhispr {version}.")

    if restart_mode == "always" or (restart_mode == "if_running" and was_running):
        start_openwhispr_non_elevated()
    return True


def verify_installation() -> int:
    if not TARGET_BIN.exists():
        raise RuntimeError(f"Binario ausente: {TARGET_BIN}")
    dry = subprocess.run(
        [str(TARGET_BIN), "--test-dry-run"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if dry.returncode != 0 or "DRY_RUN_OK" not in dry.stdout:
        raise RuntimeError(f"Falha no dry-run de {TARGET_BIN}: rc={dry.returncode}, out={dry.stdout!r}, err={dry.stderr!r}")

    validate_asar(APP_ASAR)
    archive = AsarArchive(APP_ASAR)
    checks = {
        "settingsStore": ("autoEnterEnabled", archive.read_bytes(archive.find_single(r"src/dist/assets/settingsStore-[^/]+\.js"))),
        "indexBundle": ("__owAutoEnter", archive.read_bytes(archive.find_single(r"src/dist/assets/index-[^/]+\.js"))),
        "SettingsModal": ("__owAeVal", archive.read_bytes(archive.find_single(r"src/dist/assets/SettingsModal-[^/]+\.js"))),
        "i18nBundle": ('"autoEnter":', archive.read_bytes(archive.find_single(r"src/dist/assets/i18n-[^/]+\.js"))),
        "ipcHandlers": ("pressEnter === true", archive.read_bytes("src/helpers/ipcHandlers.js")),
        "clipboard": (MARKER, archive.read_bytes("src/helpers/clipboard.js")),
        "updater": (UPDATER_MARKER, archive.read_bytes("src/updater.js")),
    }
    for name, (needle, raw) in checks.items():
        if needle not in raw.decode("utf-8", errors="ignore"):
            raise RuntimeError(f"Verificacao falhou em {name}: marcador {needle!r} ausente")
    print("VERIFY_OK: todos os 7 alvos e o binario Win32 estao integros e patcheados.")
    return 0


def cmd_guard() -> int:
    wait_for_updater_exit(90)
    if stamp_matches():
        return 0
    lock = acquire_lock()
    try:
        wait_for_updater_exit(90)
        if stamp_matches():
            return 0
        apply_patch(restart_mode="if_running", force=False)
        return 0
    finally:
        release_lock(lock)


def cmd_wait_update() -> int:
    time.sleep(3.0)
    prev_sig = None
    if STAMP_FILE.exists():
        try:
            d = json.loads(STAMP_FILE.read_text(encoding="utf-8"))
            prev_sig = (d.get("asar_size"), d.get("asar_mtime_ns"))
        except Exception:
            pass

    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        installers = running_processes(["openwhispr-setup", "installer.exe"])
        if not installers and APP_ASAR.exists():
            st = APP_ASAR.stat()
            cur_sig = (st.st_size, st.st_mtime_ns)
            if cur_sig != prev_sig:
                try:
                    if not is_asar_patched(AsarArchive(APP_ASAR)):
                        break
                except Exception:
                    pass
        time.sleep(1.5)

    lock = acquire_lock()
    try:
        wait_for_updater_exit(60)
        if APP_ASAR.exists() and not is_asar_patched(AsarArchive(APP_ASAR)):
            was_running = bool(running_processes(["openwhispr.exe"]))
            changed = apply_patch(restart_mode="always" if was_running else "if_running", force=False)
            if changed and not was_running:
                start_openwhispr_non_elevated()
        elif not stamp_matches():
            ensure_acl_and_bin()
            write_stamp()
        return 0
    finally:
        release_lock(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-patcher do recurso Auto-Enter para OpenWhispr")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="Aplica o patch imediatamente")
    group.add_argument("--guard", action="store_true", help="Verifica e reaplica silenciosamente se necessario")
    group.add_argument("--wait-update", action="store_true", help="Aguarda o instalador concluir e reaplica")
    group.add_argument("--verify", action="store_true", help="Valida integridade do app.asar patcheado")
    parser.add_argument("--force", action="store_true", help="Forca reaplicacao junto com --apply")
    args = parser.parse_args()

    try:
        if args.verify:
            return verify_installation()
        if args.guard:
            return cmd_guard()
        if args.wait_update:
            return cmd_wait_update()
        lock = acquire_lock()
        try:
            apply_patch(restart_mode="if_running", force=args.force)
            return 0
        finally:
            release_lock(lock)
    except Exception as exc:
        log(f"ERRO: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
