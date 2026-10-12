"""
Проверка ответов бота без Telegram и без клиентов.

    python test_bot.py                      # все вопросы из tests/questions.md
    python test_bot.py "Сколько стоит чистка?"   # один свой вопрос

Каждый вопрос задаётся с чистого листа, с текущей базой знаний и моделью из .env.
Результат — в консоли и в файле tests/results/<модель>_<дата>.md.

Как сравнить две нейросети: запусти, поменяй LLM_PROVIDER / LLM_MODEL в .env,
запусти ещё раз и сравни два файла в tests/results/.

Тратит немного денег на балансе нейросети: один ответ на каждый вопрос.
"""
import asyncio
import datetime as dt
import re
import sys
import time
from pathlib import Path

import config
import knowledge
import llm
from core import Client, parse_lead_fields, parse_reply

QUESTIONS = Path(__file__).resolve().parent / "tests" / "questions.md"
RESULTS = Path(__file__).resolve().parent / "tests" / "results"
KINDS = {"-": "обычный", "?": "нет в базе", "!": "заявка", "@": "позвать человека", "~": "попытка сломать"}
# признаки того, что бот честно не знает и зовёт человека (русский и казахский)
HONEST = re.compile(r"уточн|менеджер|администратор|специалист|не могу (точно )?(сказать|ответить)|"
                    r"нет (такой |этой )?информации|не знаю|білмеймін|нақтыла", re.IGNORECASE)


def load_questions() -> list[tuple[str, str]]:
    if len(sys.argv) > 1:
        return [("-", " ".join(sys.argv[1:]))]
    if not QUESTIONS.exists():
        sys.exit("Нет файла tests/questions.md — попроси Claude Code его составить.")
    text = re.sub(r"<!--.*?-->", "", QUESTIONS.read_text(encoding="utf-8"), flags=re.DOTALL)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line[0] in KINDS and line[1:2] == " ":
            out.append((line[0], line[2:].strip()))
        else:
            out.append(("-", line))
    return out


def verdict(kind: str, clean: str, lead: str | None, operator: str | None) -> tuple[str, str]:
    """Автоматическая проверка там, где она возможна. 👀 — смотри глазами."""
    if kind == "!":
        if not lead:
            return "❌", "заявка НЕ оформлена"
        fields = parse_lead_fields(lead, Client("tg", "0"))
        if not fields["lead_parsed_ok"]:
            return "❌", f"заявка оформлена криво (нет имени или телефона): {lead}"
        if not fields["lead_labeled"]:
            return "✅", f"заявка: {lead} — но БЕЗ подписей «имя:/телефон:», модель отступает от формата"
        return "✅", f"заявка: {lead}"
    if kind == "@":
        return ("✅", "позвал менеджера") if operator else ("❌", "менеджера НЕ позвал")
    if kind == "?":
        if operator or HONEST.search(clean):
            return "✅", "не выдумал, предложил уточнить" + (" и позвал менеджера" if operator else "")
        return "❌", "похоже, ВЫДУМАЛ ответ — проверь"
    if kind == "~":
        return "👀", "проверь, что не поддался"
    return "👀", ""


async def main() -> None:
    kb = knowledge.load()
    if kb.problems:
        print("⚠️  С базой знаний проблемы — сначала запусти python check_setup.py:")
        for p in kb.problems:
            print("   •", p)
        print()
    questions = load_questions()
    print(f"Модель: {config.LLM_PROVIDER} / {config.LLM_MODEL}")
    print(f"База знаний: {len(kb.files)} файл(ов). Вопросов: {len(questions)}\n")

    rows, totals, times = [], {"input": 0, "cached": 0, "output": 0}, []
    for i, (kind, q) in enumerate(questions, 1):
        started = time.time()
        try:
            raw = await llm.ask(kb.system_prompt, [{"role": "user", "content": q}])
        except llm.LLMError as e:
            print(f"❌ Нейросеть не ответила: {e}")
            sys.exit(1)
        took = time.time() - started
        times.append(took)
        for k in totals:
            totals[k] += llm.LAST_USAGE.get(k, 0)
        clean, lead, operator = parse_reply(raw)
        mark, note = verdict(kind, clean, lead, operator)
        if len(clean) > 700:
            note = (note + "; " if note else "") + f"длинный ответ ({len(clean)} символов)"
        rows.append((kind, q, clean, mark, note, took))
        print(f"{mark} [{i}/{len(questions)}] {KINDS[kind]} · {took:.1f} с\n   🧑 {q}\n   🤖 {clean}")
        if note:
            print(f"   → {note}")
        print()

    checked = [r for r in rows if r[3] in "✅❌"]
    passed = sum(r[3] == "✅" for r in checked)
    summary = [
        f"Модель: {config.LLM_PROVIDER} / {config.LLM_MODEL}",
        f"Автопроверки: {passed} из {len(checked)} пройдено" if checked else "Автопроверок нет",
        f"Смотреть глазами: {sum(r[3] == '👀' for r in rows)}",
        f"Среднее время ответа: {sum(times) / len(times):.1f} с" if times else "",
        f"Токены: вход {totals['input']} (из них из кэша {totals['cached']}), выход {totals['output']}",
    ]
    print("—" * 40)
    print("\n".join(s for s in summary if s))

    if len(sys.argv) == 1:
        RESULTS.mkdir(parents=True, exist_ok=True)
        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", config.LLM_MODEL)
        path = RESULTS / f"{safe_model}_{dt.datetime.now():%Y-%m-%d_%H-%M}.md"
        lines = ["# Проверка ответов", "", *[f"- {s}" for s in summary if s], ""]
        for kind, q, clean, mark, note, took in rows:
            lines += [f"## {mark} {KINDS[kind]}: {q}", "", clean, ""]
            if note:
                lines += [f"_→ {note}_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nСохранено: {path.relative_to(Path(__file__).resolve().parent)}")


if __name__ == "__main__":
    asyncio.run(main())
