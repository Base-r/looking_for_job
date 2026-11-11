"""Обёртка над hh-applicant-tool: обход бага потери токена при успехе.

Баг базы (пиннинг 833cb658, tool.py:1039-1066): блок
save_token()/save_cookies()/save_config() выполняется только если операция
упала исключением (или запущена с --loop) — при успехе run() возвращает код
раньше. authorize проходит успешно, OAuth-токен получен и потерян:
auth.json не создаётся, whoami даёт 403.

Обход без патча базы: подкласс с сохранением состояния в finally —
независимо от исхода операции. Этап 5 использует этот же класс.

Запуск авторизации: python -m snpr.hh_tool <email_или_телефон>
"""

from __future__ import annotations

import sys
from typing import Sequence

import urllib3

from hh_applicant_tool.tool import HHApplicantTool


def _nested_captcha_url(url: str) -> str | None:
    """hh всё чаще отдаёт не страницу капчи, а «проверку VPN»
    (/vpncheeck?backUrl=/account/captcha?state=...). Настоящая страница
    капчи — в backUrl."""
    from urllib.parse import parse_qs, urljoin, urlsplit

    query = parse_qs(urlsplit(url).query)
    if "state" in query:
        return None
    for key in ("backUrl", "backurl"):
        for back in query.get(key, []):
            if "state=" in back:
                return urljoin(url, back)
    return None


def to_png(image_data: bytes) -> bytes:
    """Окно Tk показывает только PNG/GIF, а hh присылает капчу в JPEG/WebP."""
    import io

    try:
        from PIL import Image
    except ImportError:
        return image_data
    try:
        with Image.open(io.BytesIO(image_data)) as img:
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 — не картинка: пусть Tk скажет сам
        return image_data


class SavingHHApplicantTool(HHApplicantTool):
    """HHApplicantTool, который сохраняет токен/cookies при любом исходе.

    Капчу hh всегда вводит человек: окно с картинкой появляется поверх всех
    окон, после ввода бот продолжает с того же места.
    """

    # утренний поиск идёт без вас: не дождались капчи — сохраняем оценённое
    CAPTCHA_WAIT_SECONDS = 20 * 60

    def _solve_captcha(self, image_data: bytes) -> str:
        print("\a[капча] hh просит ввести капчу — окно на экране.", flush=True)
        return self.prompt_captcha_tk(to_png(image_data))

    def prompt_captcha_tk(self, image_data: bytes) -> str:
        """Окно с капчей поверх всех окон. Не ввели за CAPTCHA_WAIT_SECONDS
        или закрыли окно — TimeoutError: прогон доделает то, что успел,
        остальное оценит в следующий раз."""
        import base64
        import tkinter as tk
        from tkinter import ttk

        result: list[str] = []
        root = tk.Tk()
        root.title("Капча hh")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        frame = ttk.Frame(root, padding=12)
        frame.pack()
        photo = tk.PhotoImage(data=base64.b64encode(image_data))
        label = ttk.Label(frame, image=photo)
        label.image = photo  # держим ссылку, иначе картинку удалит GC
        label.pack(pady=(0, 8))
        ttk.Label(frame, text="Введите текст с картинки (регистр важен)").pack()
        entry = ttk.Entry(frame, width=30, justify="center")
        entry.pack(fill="x", pady=(4, 8))

        def submit(_event: object = None) -> None:
            if text := entry.get().strip():
                result.append(text)
                root.destroy()

        ttk.Button(frame, text="Отправить", command=submit).pack(fill="x")
        root.bind("<Return>", submit)
        root.after(self.CAPTCHA_WAIT_SECONDS * 1000, root.destroy)
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_width()) // 2
        y = (root.winfo_screenheight() - root.winfo_height()) // 2
        root.geometry(f"+{x}+{y}")
        root.focus_force()
        entry.focus_set()
        root.mainloop()
        if not result:
            print("[капча] не введена — оценю остальное в следующий раз", flush=True)
            raise TimeoutError("капча не введена")
        return result[0]

    def handle_captcha(
        self,
        captcha_url: str,
        *,
        lang: str | None = None,
        referer_url: str | None = None,
    ) -> bool:
        """Как в базе, но понимает «проверку VPN» перед капчей: капчу
        всё равно вводите вы, в окне с картинкой."""
        from urllib.parse import parse_qs, urlsplit

        from hh_applicant_tool.constants import (
            ANDROID_WEBVIEW_USERAGENT,
            CAPTCHA_SUCCESS_BACKURL,
        )

        session = self._create_session(user_agent=ANDROID_WEBVIEW_USERAGENT)
        r = session.get(
            captcha_url,
            {"backurl": CAPTCHA_SUCCESS_BACKURL},
            headers={"Referer": referer_url} if referer_url else {},
        )
        r.raise_for_status()
        # «VPN мешает работе сайта»: страница капчи и её state — в backUrl;
        # саму капчу показываем вам обычным окном с картинкой
        page_url = _nested_captcha_url(r.url) or r.url
        query = parse_qs(urlsplit(page_url).query)
        if "state" not in query:
            raise ValueError(f"В ссылке на капчу нет state: {r.url}")
        state = query["state"][0]
        xsrf_token = session.cookies.get("_xsrf")
        if not xsrf_token:
            raise ValueError("xsrf token отсутствует в Cookies!")
        solution = self.solve_captcha(
            captcha_state=state,
            lang=lang,
            referer_url=page_url,
            xsrf_token=xsrf_token,
            session=session,
        )
        ok = self._send_captcha(
            text=solution.text,
            key=solution.key,
            state=solution.state,
            referer_url=page_url,
            xsrf_token=xsrf_token,
            backurl=CAPTCHA_SUCCESS_BACKURL,
            session=session,
        )
        print("[капча] принята" if ok else "[капча] неверно, будет новая", flush=True)
        return ok

    def run(self, argv: Sequence[str] | None = None) -> int | None:
        try:
            return super().run(argv)
        finally:
            self._persist()

    def _persist(self) -> None:
        try:
            if self.save_token():
                print("Токен сохранён в auth.json")
        except Exception as ex:
            print(f"Не удалось сохранить токен: {ex}")
        try:
            self.save_cookies()
        except Exception as ex:
            print(f"Не удалось сохранить cookies: {ex}")
        try:
            self._save_config_if_needed()
        except Exception:
            pass


def build_auth_argv(argv: Sequence[str]) -> list[str]:
    # Капча вручную: AI-креды базы не настроены, наш SNE_AI_* ей не читается.
    return ["--captcha-manual", "authorize", *argv]


def main(argv: Sequence[str] | None = None) -> int:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    args = list(argv) if argv is not None else sys.argv[1:]
    tool = SavingHHApplicantTool()
    ret = tool.run(build_auth_argv(args))
    return ret if isinstance(ret, int) else 0


if __name__ == "__main__":
    sys.exit(main())
