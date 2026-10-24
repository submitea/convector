"""Точка входа. Вызывается из контекстного меню: main.py "путь\к\файлу"

Если выделено несколько файлов, Windows запускает программу по разу на каждый.
Чтобы не плодить окна, первый процесс становится «сервером», а остальные
просто передают ему свои пути и закрываются.
"""
import json
import queue
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

PORT = 47615
APP_DIR = Path(__file__).resolve().parent


def become_server(paths):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", PORT))
        srv.listen(32)
        return srv
    except OSError:
        srv.close()
    for _ in range(25):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1) as s:
                s.sendall((json.dumps(paths) + "\n").encode("utf-8"))
            return False  # передали файлы уже открытому окну
        except OSError:
            time.sleep(0.1)
    return None  # порт занят чужой программой, работаем автономно


def serve(srv, inbox):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            data = b""
            try:
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                inbox.put(json.loads(data.decode("utf-8")))
            except Exception:
                pass


def main():
    paths = [str(Path(a).resolve()) for a in sys.argv[1:] if Path(a).is_file()]
    inbox = queue.Queue()
    srv = None
    if paths:
        srv = become_server(paths)
        if srv is False:
            return          # файлы переданы уже открытому окну — выходим мгновенно
        if srv:
            threading.Thread(target=serve, args=(srv, inbox), daemon=True).start()
    from gui import App
    App(paths, inbox, standby=bool(srv)).mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw не показывает ошибки, поэтому пишем в лог и показываем окно
        msg = traceback.format_exc()
        (APP_DIR / "error.log").write_text(msg, encoding="utf-8")
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("Конвертер", msg[-900:])
        except Exception:
            pass
