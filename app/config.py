import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchConfig:
    text: str = "Python"
    areas: tuple[str, ...] = ("16",)
    experience: str | None = None
    salary: int | None = None
    currency: str = "BYR"
    excluded_title_words: tuple[str, ...] = ()
    excluded_employer_ids: tuple[str, ...] = ()
    work_formats: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Конфигурация должна быть JSON-объектом")
        data = dict(data)
        unknown = data.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
        for key in ("areas", "excluded_title_words", "excluded_employer_ids", "work_formats"):
            if key in data:
                if not isinstance(data[key], list) or not all(isinstance(x, str) for x in data[key]):
                    raise ValueError(f"{key}: ожидается массив строк")
                data[key] = tuple(data[key])
        obj = cls(**data)
        if not isinstance(obj.text, str) or not obj.text.strip():
            raise ValueError("Нужен непустой text")
        if obj.salary is not None and (type(obj.salary) is not int or obj.salary < 0):
            raise ValueError("salary: ожидается неотрицательное целое число")
        if obj.experience not in (None, "noExperience", "between1And3", "between3And6", "moreThan6"):
            raise ValueError("Неизвестное значение experience")
        if not isinstance(obj.currency, str) or not obj.currency:
            raise ValueError("currency: ожидается код валюты")
        if any(x not in ("ON_SITE", "REMOTE", "HYBRID", "FIELD_WORK") for x in obj.work_formats):
            raise ValueError("Неизвестный формат работы")
        return obj

    def save(self, path):
        # Validate the entire document and replace it atomically for concurrent readers.
        document = json.loads(json.dumps(asdict(self)))
        self.from_dict(document)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix=target.name + ".", suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def params(self):
        params = {"host": "rabota.by", "text": self.text,
                  "order_by": "publication_time"}
        if self.areas:
            params["area"] = self.areas
        if self.experience:
            params["experience"] = self.experience
        if self.salary is not None:
            params.update(salary=self.salary, currency=self.currency)
        if self.work_formats:
            params["work_format"] = self.work_formats
        return params

    def rejection(self, vacancy):
        title = vacancy.get("name", "").casefold()
        if any(word.casefold() in title for word in self.excluded_title_words if word):
            return "excluded_title_word"
        employer = vacancy.get("employer") or {}
        if str(employer.get("id", "")) in self.excluded_employer_ids:
            return "excluded_employer"
        return None
