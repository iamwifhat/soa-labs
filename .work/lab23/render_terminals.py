"""Готує HTML-сторінки «вікно терміналу» зі збереженого виводу перевірочних сценаріїв ЛР2 і ЛР3.

Текст береться без змін із файлів .work/lab2/outputs і .work/lab3/outputs; змінюється лише розбиття на частини.
"""
import html
import json
import re
from pathlib import Path

WORK = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "terminals"
OUT.mkdir(exist_ok=True)


def sections(text: str) -> dict[int, str]:
    parts = re.split(r"\n(?==== \d+\.)", "\n" + text)
    result = {}
    for part in parts:
        match = re.match(r"=== (\d+)\.", part.strip())
        if match:
            result[int(match.group(1))] = part.strip("\n")
    return result


def total(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("Перевірок:"))


lab2_demo = (WORK / "lab2/outputs/demo.txt").read_text()
lab2_part1 = (WORK / "lab2/outputs/scaling_part1.txt").read_text().strip("\n")
lab2_part2 = (WORK / "lab2/outputs/scaling_part2.txt").read_text().strip("\n")


def stages(text: str) -> list[str]:
    return [part.strip("\n") for part in re.split(r"\n(?==== )", "\n" + text) if part.strip()]


s1, s2 = stages(lab2_part1), stages(lab2_part2)
lab2_breaker = (WORK / "lab2/outputs/breaker_logs.txt").read_text().strip("\n")
lab3_demo = (WORK / "lab3/outputs/saga_demo.txt").read_text()
d2, d3 = sections(lab2_demo), sections(lab3_demo)
first_kv = lab3_demo.strip("\n").splitlines()[0]


def breaker_log(line: str) -> str:
    # Скорочено лише службовий префікс: дата, рівень і назва логера; повідомлення без змін.
    match = re.match(r"\S+ (\S+) \S+ (\S+) \[[^\]]+\] (.*)", line)
    return f"{match.group(1)[:8]} {match.group(2)}: {match.group(3)}" if match else line


terminals = {
    "lab2-demo-1": ("lab2", "python3 scripts/demo.py", "\n\n".join([d2[1], d2[2]])),
    "lab2-demo-2": ("lab2", "python3 scripts/demo.py   (продовження)", "\n\n".join([d2[3], d2[4], d2[5]])),
    "lab2-demo-3": ("lab2", "python3 scripts/demo.py   (продовження)", "\n\n".join([d2[6], d2[7], d2[8]])),
    "lab2-scaling-1": ("lab2", "python3 scripts/scaling_demo.py up", s1[0]),
    "lab2-scaling-2": ("lab2", "python3 scripts/scaling_demo.py lb consumers", "\n\n".join(s1[1:])),
    "lab2-scaling-3": ("lab2", "python3 scripts/scaling_demo.py failover down", "\n\n".join(s2[:2])),
    "lab2-scaling-4": ("lab2", "python3 scripts/scaling_demo.py breaker",
                       s2[2] + "\n\n$ docker compose logs calendar-service | grep 'Circuit breaker'\n"
                       + "\n".join(breaker_log(line) for line in lab2_breaker.splitlines())),
    "lab3-saga-1": ("lab3", "python3 scripts/saga_demo.py", "\n\n".join([first_kv, d3[1], d3[2]])),
    "lab3-saga-2": ("lab3", "python3 scripts/saga_demo.py   (продовження)", "\n\n".join([d3[3], d3[4]])),
    "lab3-saga-3": ("lab3", "python3 scripts/saga_demo.py   (продовження)", "\n\n".join([d3[5], d3[6]])),
    "lab3-saga-4": ("lab3", "python3 scripts/saga_demo.py   (продовження)", d3[7]),
}

STYLE = """
html,body{margin:0;background:white}
.term{width:90ch;border:2px solid #9a9a9a;font-family:Menlo,'DejaVu Sans Mono',monospace;font-size:15px;color:#111;background:#fff}
.bar{background:#ececec;border-bottom:1px solid #b5b5b5;padding:6px 12px;font-family:'Helvetica Neue',Arial,sans-serif;font-size:14px;color:#333}
pre{margin:0;padding:10px 14px 14px;white-space:pre-wrap;word-break:break-word;font:inherit;line-height:1.35}
.cmd{font-weight:bold}
"""

manifest = {}
for name, (project, command, body) in terminals.items():
    lines = []
    for line in body.splitlines():
        escaped = html.escape(line)
        lines.append(f'<span class="cmd">{escaped}</span>' if line.startswith("$ ") else escaped)
    page = (f'<!doctype html><html lang="uk"><meta charset="utf-8"><title>{name}</title><style>{STYLE}</style>'
            f'<div class="term" id="term"><div class="bar">{project} — zsh</div>'
            f'<pre><span class="cmd">$ {html.escape(command)}</span>\n' + "\n".join(lines) + '</pre></div></html>')
    (OUT / f"{name}.html").write_text(page)
    manifest[name] = len(body.splitlines())
print(json.dumps(manifest, ensure_ascii=False, indent=1))
