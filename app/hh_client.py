import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status

    @property
    def public_message(self):
        # Only controlled text is forwarded to Telegram; never raw server bodies.
        if self.status == 403:
            return ("HH API: HTTP 403 — доступ к поиску вакансий запрещён. "
                    "Проверьте доступ приложения HH и HH_ACCESS_TOKEN в .env. "
                    "Если токен уже задан, проверьте его права. Изменение фильтров не устраняет отказ в доступе.")
        if self.status == 401:
            return "HH API: HTTP 401 — требуется действующий HH_ACCESS_TOKEN. Проверьте или обновите токен в .env."
        if self.status == 400:
            return "HH API: HTTP 400 — запрос отклонён. Проверьте фильтры и HH_USER_AGENT."
        if self.status == 429:
            return "HH API: HTTP 429 — превышена частота запросов. Повторите поиск позднее."
        if self.status:
            return f"HH API: HTTP {self.status}. Повторите позднее; при повторении проверьте доступ приложения."
        return "Не удалось завершить сбор HH: ошибка сети, ответа или ограничение сбора. Проверьте доступность api.hh.ru и повторите позднее."


class HHClient:
    """Only GET to HH vacancy search; optional Bearer access token, no cookies."""

    def __init__(self, user_agent, *, access_token="", timeout=20, attempts=3, opener=urlopen, sleep=time.sleep):
        if not user_agent or "@" not in user_agent or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("Укажите HH_USER_AGENT: JobAssistant/0.1 (ваш-email)")
        self.user_agent = user_agent
        if not isinstance(access_token, str) or not access_token.isascii() or any(c.isspace() for c in access_token):
            raise ValueError("HH_ACCESS_TOKEN должен содержать только токен без пробелов")
        self.access_token = access_token
        self.timeout, self.attempts = timeout, attempts
        self.opener, self.sleep = opener, sleep

    def search(self, params):
        url = "https://api.hh.ru/vacancies?" + urlencode(params, doseq=True)
        headers = {"HH-User-Agent": self.user_agent, "User-Agent": self.user_agent, "Accept": "application/json"}
        request = Request(url, headers=headers, method="GET")
        if self.access_token:
            # Do not copy the credential to redirect targets.
            request.add_unredirected_header("Authorization", "Bearer " + self.access_token)
        for attempt in range(self.attempts):
            status = None
            retry_after = None
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    data = json.load(response)
                if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                        or any(type(data.get(k)) is not int or data[k] < 0 for k in ("pages", "found"))
                        or any(not isinstance(x, dict) or not x.get("id") for x in data["items"])):
                    raise ApiError("Неожиданный формат ответа HH")
                return data
            except HTTPError as exc:
                code = exc.code
                status = code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if code != 429 and not 500 <= code < 600:
                    raise ApiError(f"HH API: HTTP {code}", status=code) from None
                error = f"HH API: HTTP {code}"
            except (URLError, TimeoutError, OSError) as exc:
                error = f"Ошибка сети: {type(exc).__name__}"
            except (ValueError, UnicodeError):
                raise ApiError("HH вернул некорректный JSON") from None
            if attempt + 1 == self.attempts:
                raise ApiError(f"{error}; исчерпаны {self.attempts} попытки", status=status)
            delay = 2 ** attempt + random.random()
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            if delay > 60:
                raise ApiError("API просит подождать более минуты; повторите сбор позднее", status=status)
            self.sleep(delay)
        raise ApiError("Не удалось выполнить запрос")
