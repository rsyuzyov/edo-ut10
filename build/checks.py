#!/usr/bin/env python3
"""Проверки исходников расширения, не требующие платформы 1С.

Собрать .cfe без конфигуратора нельзя, поэтому в CI проверяется то, что читается
из самих XML: версия расширения, версия формата выгрузки, разбираемость XML
и отсутствие в публичном дереве следов конкретной базы.

Запуск:
    python build/checks.py            # все проверки
    python build/checks.py --version  # напечатать версию расширения и выйти
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

# Формат выгрузки держим в 2.17 (платформы 8.3.20–8.3.24): старшая платформа читает
# более ранние форматы, младшая новые — нет. Выгрузка старшей платформой поднимет
# версию молча и закроет исходники для 8.3.x.
MAX_DUMP_FORMAT = (2, 17)

MD_NS = "{http://v8.1c.ru/8.3/MDClasses}"

# Следы конкретной базы, которым не место в публичном дереве.
HYGIENE_PATTERNS = [
    (
        "имя внутреннего домена",
        re.compile(r"\b[a-z0-9-]+\.(?:local|lan|corp)\b", re.IGNORECASE),
    ),
    (
        # 10.0.0.0/8 сюда не входит намеренно: версии конфигураций 1С («УТ 10.3.88.3»)
        # от таких адресов неотличимы и дают ложные срабатывания в каждом прогоне.
        "приватный IP-адрес",
        re.compile(r"\b(?:192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ),
    (
        "непустой отпечаток КЭП в коде",
        re.compile(r"Отпечаток\w*\s*=\s*\"[0-9A-Za-z+/=]{16,}\""),
    ),
    (
        "пароль строкой",
        re.compile(r"(?:Пароль|Password)\s*=\s*\"[^\"]{3,}\"", re.IGNORECASE),
    ),
    (
        "номер реального документа",
        re.compile(r"\b[А-Я]{2}-[А-Я]?\d{6,}\b"),
    ),
    (
        "ИНН, ОГРН или EAN в комментарии",
        re.compile(r"//.*(?<![\d.,])(?!1234567890)(?:\d{10}|\d{12,13}|\d{15})(?![\d.,])"),
    ),
]

# Закрытый список слов (площадки, организации): не хранится в публичном дереве,
# в CI приезжает секретом, значения разделены «;». В журнал слово не печатается.
PRIVATE_WORDS = [
    re.compile(r"\b" + re.escape(word.strip()), re.IGNORECASE)
    for word in os.environ.get("HYGIENE_DENYLIST", "").split(";")
    if word.strip()
]

TEXT_SUFFIXES = (".xml", ".bsl", ".txt", ".mdo", ".html", ".md", ".py", ".ps1", ".yml", ".yaml", ".xsd")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src_dir() -> str:
    return os.path.join(repo_root(), "src")


def iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def rel(path: str) -> str:
    return os.path.relpath(path, repo_root()).replace(os.sep, "/")


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, message: str) -> None:
        print("  OK   " + message)

    def fail(self, message: str) -> None:
        self.failed = True
        print("  FAIL " + message)

    def stage(self, title: str) -> None:
        print("==> " + title)


def read_version(report: Report | None = None) -> str | None:
    """Версия расширения из src/Configuration.xml."""
    path = os.path.join(src_dir(), "Configuration.xml")
    if not os.path.exists(path):
        if report:
            report.fail("не найден " + rel(path))
        return None
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        if report:
            report.fail("Configuration.xml не разбирается: %s" % exc)
        return None
    node = tree.getroot().find(
        "./{0}Configuration/{0}Properties/{0}Version".format(MD_NS)
    )
    if node is None or not (node.text or "").strip():
        if report:
            report.fail("в Configuration.xml не заполнен <Version>")
        return None
    return node.text.strip()


def check_version(report: Report) -> str | None:
    report.stage("версия расширения")
    version = read_version(report)
    if version is None:
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        report.fail("версия %r не в формате N.N.N.N" % version)
        return None
    report.ok("версия %s" % version)
    return version


def check_dump_format(report: Report) -> None:
    """Версия формата выгрузки не должна уезжать выше MAX_DUMP_FORMAT."""
    report.stage("версия формата выгрузки")
    seen: dict[str, list[str]] = {}
    for path in iter_files(src_dir()):
        if not path.endswith(".xml"):
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue  # о разборе отчитывается отдельная проверка
        value = root.get("version")
        if value:
            seen.setdefault(value, []).append(rel(path))
    if not seen:
        report.fail("ни в одном XML нет атрибута version — дерево выгрузки не опознано")
        return
    limit = "%d.%d" % MAX_DUMP_FORMAT
    for value, files in sorted(seen.items()):
        try:
            parsed = tuple(int(part) for part in value.split("."))
        except ValueError:
            report.fail("нечитаемая версия формата %r (%s)" % (value, files[0]))
            continue
        if parsed > MAX_DUMP_FORMAT:
            report.fail(
                "формат выгрузки %s > %s в %d файле(ах), первый — %s; "
                "дерево выгружено слишком новой платформой"
                % (value, limit, len(files), files[0])
            )
        else:
            report.ok("формат %s — %d файл(ов)" % (value, len(files)))


def check_text_template_bom(report: Report) -> None:
    """Текстовые макеты (Templates/**/Ext/*.txt) обязаны начинаться с BOM.

    Без BOM ПолучитьМакет().ПолучитьТекст() в сеансе читает макет как ANSI,
    и настройки маппинга превращаются в кракозябры с невалидным XML внутри
    (прецедент: BOM потерялся при правке, ФЛК и отправка встали).
    """
    report.stage("BOM у текстовых макетов")
    checked = 0
    missing = 0
    for path in iter_files(src_dir()):
        if not path.endswith(".txt") or os.sep + "Templates" + os.sep not in path:
            continue
        checked += 1
        with open(path, "rb") as handle:
            head = handle.read(3)
        if head != b"\xef\xbb\xbf":
            missing += 1
            report.fail("%s: нет BOM — макет прочитается как ANSI" % rel(path))
    if checked and not missing:
        report.ok("BOM на месте — %d текстовых макетов" % checked)
    elif not checked:
        report.ok("текстовых макетов нет — проверка неприменима")


def check_xml_parses(report: Report) -> None:
    report.stage("разбор XML")
    total = 0
    for path in iter_files(src_dir()):
        if not path.endswith(".xml"):
            continue
        total += 1
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            report.fail("%s: %s" % (rel(path), exc))
    if total:
        report.ok("разобрано %d XML" % total)
    else:
        report.fail("в src не найдено ни одного XML")


def hygiene_sources():
    """Текст всех файлов дерева, включая вложенные в zip-макеты: (имя, текст)."""
    for path in iter_files(repo_root()):
        relative = rel(path).replace(os.sep, "/")
        if relative.startswith(".git/") or relative == "build/checks.py":
            continue
        if path.lower().endswith(TEXT_SUFFIXES):
            try:
                with open(path, "r", encoding="utf-8-sig", errors="strict") as handle:
                    yield rel(path), handle.read()
            except (UnicodeDecodeError, OSError):
                continue
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for number, entry in enumerate(archive.infolist(), 1):
                    payload = archive.read(entry)
                    try:
                        yield "%s#%d" % (rel(path), number), payload.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        yield "%s#%d" % (rel(path), number), payload.decode("cp1251", "replace")


def check_hygiene(report: Report) -> None:
    """Следы конкретной базы в публичном дереве."""
    report.stage("гигиена публичного дерева")
    hits = 0
    for name, text in hygiene_sources():
        for line_no, line in enumerate(text.splitlines(), 1):
            for title, pattern in HYGIENE_PATTERNS:
                match = pattern.search(line)
                if match:
                    hits += 1
                    report.fail("%s:%d — %s: %s" % (name, line_no, title, match.group(0)[:80]))
            if any(word.search(line) for word in PRIVATE_WORDS):
                hits += 1
                report.fail("%s:%d — слово из закрытого списка" % (name, line_no))
    if not hits:
        report.ok("следов конкретной базы не найдено")


def git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ("git",) + args,
        cwd=repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip()


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def last_release_tag() -> str | None:
    code, out = git("tag", "--list", "v*", "--sort=-v:refname")
    if code != 0 or not out:
        return None
    for line in out.splitlines():
        if re.fullmatch(r"v\d+\.\d+\.\d+\.\d+", line.strip()):
            return line.strip()
    return None


def check_version_bumped(report: Report, version: str | None) -> None:
    """Изменился src — версия обязана быть выше последнего выпущенного тега.

    Ровно тот дефект, ради которого проверка и стоит: в двух базах оказались разные
    сборки с одинаковым номером, и отличить их было нечем.
    """
    report.stage("версия поднята относительно последнего релиза")
    if version is None:
        report.fail("версия не прочитана — проверка пропущена")
        return
    code, _ = git("rev-parse", "--git-dir")
    if code != 0:
        report.ok("не git-репозиторий — проверка неприменима")
        return
    tag = last_release_tag()
    if tag is None:
        report.ok("релизных тегов ещё нет — сравнивать не с чем")
        return
    code, _ = git("diff", "--quiet", tag, "HEAD", "--", "src")
    if code == 0:
        report.ok("src не менялся с %s" % tag)
        return
    if code != 1:
        report.ok("сравнение с %s недоступно (нет полной истории) — пропущено" % tag)
        return
    released = version_tuple(tag[1:])
    if version_tuple(version) <= released:
        report.fail(
            "src изменён после %s, а версия так и осталась %s — поднять <Version> "
            "в src/Configuration.xml" % (tag, version)
        )
    else:
        report.ok("%s > %s, src изменён — версия поднята" % (version, tag))


def base_ref() -> str | None:
    """Ветка, в которую идёт изменение: в PR на GitHub — его base, локально — origin/main.

    На самом main (push после слияния) сравнивать не с чем: проверка выключается.
    """
    pull_request_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    candidates = ["origin/" + pull_request_base] if pull_request_base else []
    if not pull_request_base:
        code, current_branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if (code == 0 and current_branch == "main") or os.environ.get("GITHUB_EVENT_NAME") == "push":
            return None
        candidates.append("origin/main")
    for candidate in candidates:
        code, _ = git("rev-parse", "--verify", "--quiet", candidate + "^{commit}")
        if code == 0:
            return candidate
    return None


def check_version_above_base(report: Report, version: str | None) -> None:
    """Изменился src — версия обязана быть выше той, что уже стоит в целевой ветке.

    Сравнения с релизным тегом мало: PR с номером, уже занятым в main, проходил зелёным,
    и после слияния в main оказывались две разные сборки с одним номером.
    """
    report.stage("версия выше, чем в целевой ветке")
    if version is None:
        report.fail("версия не прочитана — проверка пропущена")
        return
    target = base_ref()
    if target is None:
        report.ok("целевой ветки нет (main или не PR) — проверка неприменима")
        return
    code, _ = git("diff", "--quiet", target, "HEAD", "--", "src")
    if code == 0:
        report.ok("src не отличается от %s" % target)
        return
    if code != 1:
        report.ok("сравнение с %s недоступно (нет полной истории) — пропущено" % target)
        return
    code, target_configuration = git("show", target + ":src/Configuration.xml")
    match = re.search(r"<Version>\s*([\d.]+)\s*</Version>", target_configuration) if code == 0 else None
    if match is None:
        report.fail("версия в %s не прочитана" % target)
        return
    target_version = match.group(1)
    if version_tuple(version) <= version_tuple(target_version):
        report.fail(
            "src отличается от %s, а версия %s не выше, чем там (%s) — поднять <Version> "
            "в src/Configuration.xml" % (target, version, target_version)
        )
    else:
        report.ok("%s > %s в %s" % (version, target_version, target))


METHOD_DECLARATION = re.compile(
    r"^\s*(?:Асинх\s+|Async\s+)?(?:Процедура|Функция|Procedure|Function)\s+([\wА-Яа-яЁё]+)\s*\(",
    re.IGNORECASE,
)


def check_duplicate_methods(report: Report) -> None:
    """Два метода с одним именем в модуле: платформа не компилирует модуль целиком.

    XML-проверки этого не видят, а ошибка всплывает только при сборке или загрузке
    (прецедент: после слияния в общем модуле оказались две функции с одним именем
    и разным смыслом).
    """
    report.stage("повтор имён процедур и функций в модуле")
    checked = 0
    hits = 0
    for path in iter_files(src_dir()):
        if not path.lower().endswith(".bsl"):
            continue
        checked += 1
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = handle.read().splitlines()
        first_line_by_name: dict[str, int] = {}
        for line_no, line in enumerate(lines, 1):
            match = METHOD_DECLARATION.match(line)
            if match is None:
                continue
            method_key = match.group(1).casefold()
            if method_key in first_line_by_name:
                hits += 1
                report.fail(
                    "%s:%d — %s уже объявлен в строке %d"
                    % (rel(path), line_no, match.group(1), first_line_by_name[method_key])
                )
            else:
                first_line_by_name[method_key] = line_no
    if not hits:
        report.ok("повторов нет — %d модул(ей)" % checked)


def check_child_objects(report: Report) -> None:
    """Каждый объект из src/<Вид>/<Имя>.xml зарегистрирован в ChildObjects Configuration.xml, и наоборот.

    Без регистрации объект не попадает в расширение, а вызовы его модуля роняют компиляцию
    вызывающих (прецедент: при слиянии main конфликт строки версии разрешили целиком в пользу
    main и потеряли соседнюю строку <CommonModule>).
    """
    report.stage("состав ChildObjects и объекты в src")
    try:
        configuration_root = ET.parse(os.path.join(src_dir(), "Configuration.xml")).getroot()
    except (ET.ParseError, OSError) as error:
        report.fail("Configuration.xml не прочитан: %s" % error)
        return
    child_objects = configuration_root.find(".//" + MD_NS + "ChildObjects")
    if child_objects is None:
        report.fail("в Configuration.xml нет ChildObjects")
        return
    registered = {
        (child.tag.replace(MD_NS, ""), (child.text or "").strip()) for child in child_objects
    }
    on_disk: dict[tuple[str, str], str] = {}
    for kind_dir in sorted(os.listdir(src_dir())):
        kind_path = os.path.join(src_dir(), kind_dir)
        if not os.path.isdir(kind_path):
            continue
        for file_name in sorted(os.listdir(kind_path)):
            object_file = os.path.join(kind_path, file_name)
            if not file_name.endswith(".xml") or not os.path.isfile(object_file):
                continue
            try:
                object_root = ET.parse(object_file).getroot()
            except ET.ParseError:
                continue  # о разборе отчитывается отдельная проверка
            object_elements = list(object_root)
            if not object_elements:
                continue
            object_kind = object_elements[0].tag.replace(MD_NS, "")
            on_disk[(object_kind, file_name[: -len(".xml")])] = rel(object_file)
    missing_registration = sorted(set(on_disk) - registered)
    missing_files = sorted(registered - set(on_disk))
    for object_kind, object_name in missing_registration:
        report.fail(
            "%s есть в src, но не зарегистрирован в ChildObjects: <%s>%s</%s>"
            % (on_disk[(object_kind, object_name)], object_kind, object_name, object_kind)
        )
    for object_kind, object_name in missing_files:
        report.fail("в ChildObjects есть <%s>%s</%s>, а файла объекта нет" % (object_kind, object_name, object_kind))
    if not missing_registration and not missing_files:
        report.ok("совпадает — %d объект(ов)" % len(registered))


def main() -> int:
    # Отчёт на русском: на Windows консоль по умолчанию не UTF-8, и без этого
    # вывод приезжает вопросиками (в cmd понадобится ещё `chcp 65001`).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="store_true",
        help="напечатать версию расширения и выйти",
    )
    args = parser.parse_args()

    if args.version:
        version = read_version()
        if version is None:
            print("версия не прочитана", file=sys.stderr)
            return 1
        print(version)
        return 0

    report = Report()
    version = check_version(report)
    check_dump_format(report)
    check_text_template_bom(report)
    check_xml_parses(report)
    check_hygiene(report)
    check_duplicate_methods(report)
    check_child_objects(report)
    check_version_bumped(report, version)
    check_version_above_base(report, version)

    print()
    if report.failed:
        print("проверки НЕ пройдены")
        return 1
    print("проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
