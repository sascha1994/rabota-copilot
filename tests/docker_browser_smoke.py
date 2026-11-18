"""Run inside the Docker image with --network none and a disposable data directory."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from app.browser_sender import BrowserSender


RESUME = "1234567890abcdef1234"
FORM = f"""
<meta charset="utf-8">
<button onclick="this.hidden=true;document.querySelector('section').hidden=false">Откликнуться</button>
<section hidden>
  <h1>Отклик на вакансию</h1>
  <button>Веб-дизайнер</button>
  <label><input type="radio" value="{RESUME}">Тестовое резюме</label>
  <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
  <button data-qa="vacancy-response-submit-popup" onclick="
    if (document.querySelector('input').checked && document.querySelector('textarea').value === 'Тест') {{
      localStorage.setItem('submitted', 'yes');
      document.body.textContent = 'Отклик отправлен';
    }}
  ">Отправить</button>
</section>
"""


class FixtureSender(BrowserSender):
    @contextmanager
    def _browser_context(self):
        with super()._browser_context() as context:
            context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=FORM))
            yield context


if __name__ == "__main__":
    with TemporaryDirectory(dir="/app/data") as temp:
        profile = Path(temp) / "profile"
        profile.mkdir()
        sender = FixtureSender({"BROWSER_CHANNEL": "chromium", "BROWSER_PROFILE_DIR": str(profile),
                                "RABOTA_RESUME_WEB": RESUME})
        receipt = sender.send({"vacancy_id": "10", "metadata_json": '{"direction":"web"}',
                               "job_id": 1, "revision": 1, "letter_text": "Тест"})
        assert receipt["resume_id"] == RESUME and Path(receipt["screenshot"]).is_file()
        with sender._browser_context() as context:
            page = context.pages[0]
            page.goto("https://rabota.by/vacancy/10")
            assert page.evaluate("localStorage.getItem('submitted')") == "yes"
        print("Verified headed Chromium, sandboxed submission fixture, receipt and persistent profile")
