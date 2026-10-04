"""質問セットと例示の読み込み。"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from . import config


@dataclass
class Question:
    id: str
    category: str
    question: str
    gold_sql: str
    ordered: bool = False
    metrics_answerable: bool = True
    reference_metrics: dict | None = None
    notes: str = ""
    extra: dict = field(default_factory=dict)


def _mk(d: dict) -> Question:
    keys = {"id", "category", "question", "gold_sql", "ordered", "metrics_answerable", "reference_metrics", "notes"}
    return Question(**{k: v for k, v in d.items() if k in keys}, extra={k: v for k, v in d.items() if k not in keys})


def load_spec(path=config.QUESTIONS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_questions(path=config.QUESTIONS) -> list[Question]:
    return [_mk(d) for d in load_spec(path)["questions"]]


def load_smoke(path=config.QUESTIONS) -> Question:
    return _mk(load_spec(path)["smoke"])


def repro_ids(path=config.QUESTIONS) -> list[str]:
    return list(load_spec(path)["repro_questions"])


def categories(path=config.QUESTIONS) -> dict[str, str]:
    return dict(load_spec(path)["categories"])


def load_examples() -> list[dict]:
    return yaml.safe_load((config.SEMANTIC / "examples.yaml").read_text(encoding="utf-8"))["examples"]
