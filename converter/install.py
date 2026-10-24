"""Установка / удаление пункта «Конвертировать…» в контекстном меню Windows.

  python install.py                      добавить пункт в меню
  python install.py --uninstall          убрать пункт
  python install.py --classic-menu on    вернуть полное старое меню Win11 (без «Показать доп. параметры»)
  python install.py --classic-menu off   вернуть новое меню Win11

Права администратора не нужны: всё пишется в HKEY_CURRENT_USER.
"""
import argparse
import ctypes
import subprocess
import sys
import winreg
from pathlib import Path

import core

APP_DIR = Path(__file__).resolve().parent
KEY = "ConverterPro"
CLASSIC_CLSID = r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"


def notify_shell():
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def install():
    pyw = Path(sys.executable)
    if pyw.name.lower() == "python.exe":
        pyw = pyw.with_name("pythonw.exe")
    main = APP_DIR / "main.py"
    icon = APP_DIR / "assets" / "icon.ico"
    for ext in sorted(core.ALL_EXT):
        base = rf"Software\Classes\SystemFileAssociations\.{ext}\shell\{KEY}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Конвертировать…")
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, str(icon))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\command") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, f'"{pyw}" "{main}" "%1"')
    notify_shell()
    print(f"Готово: пункт добавлен для {len(core.ALL_EXT)} типов файлов.")
    print("В Windows 11 он находится в «Показать дополнительные параметры» (Shift+F10).")


def _delete_tree(root, path):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + sub)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def uninstall():
    for ext in core.ALL_EXT:
        _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\SystemFileAssociations\.{ext}\shell\{KEY}")
    notify_shell()
    print("Пункт меню удалён.")


def classic_menu(on: bool):
    if on:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLASSIC_CLSID + r"\InprocServer32") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, "")
    else:
        _delete_tree(winreg.HKEY_CURRENT_USER, CLASSIC_CLSID)
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True)
    subprocess.Popen(["explorer.exe"])
    print("Классическое меню " + ("включено" if on else "выключено") + " (Проводник перезапущен).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--classic-menu", choices=["on", "off"])
    a = ap.parse_args()
    if a.classic_menu:
        classic_menu(a.classic_menu == "on")
    elif a.uninstall:
        uninstall()
    else:
        install()
