import argparse
import shutil
import sys
import pathlib
import unicodedata
import logging
import time
from datetime import datetime
from shutil import unpack_archive

from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ---------------------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------------------

# Папка для логов создаётся рядом со скриптом
script_dir = pathlib.Path(__file__).parent
logs_dir = script_dir / "Logs"
logs_dir.mkdir(exist_ok=True)

# Имя файла лога с датой
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_file = logs_dir / f"organizer_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

logging.info("=== File Organizer started ===")
logging.info(f"Лог-файл: {log_file}")

app = Flask(__name__)
HTTP_REQUESTS = Counter(
    "organizer_http_requests_total",
    "Кількість HTTP-запитів до організатора.",
    ("method", "path", "status"),
)
HTTP_DURATION = Histogram(
    "organizer_http_request_duration_seconds",
    "Час виконання HTTP-запитів організатора.",
    ("method", "path"),
)
HTTP_IN_PROGRESS = Gauge(
    "organizer_http_requests_in_progress",
    "Кількість активних HTTP-запитів.",
)
ORGANIZE_OPERATIONS = Counter(
    "organizer_operations_total",
    "Кількість запусків операції класифікації файлів.",
    ("result",),
)

# ---------------------- КАТЕГОРИИ ----------------------
CATEGORIES = {
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "svg", "webp", "heic"},
    "Video": {"mp4", "mkv", "mov", "avi", "flv", "wmv", "webm"},
    "Audio": {"mp3", "wav", "ogg", "flac", "aac", "m4a"},
    "Documents": {"pdf", "doc", "docx", "txt", "odt", "xls", "xlsx", "ppt", "pptx", "rtf", "md"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"},
    "Code": {"py", "js", "java", "c", "cpp", "cs", "html", "css", "go", "rs", "php", "rb"},
}

ARCHIVE_EXTS = {"zip", "tar", "gz", "tgz", "bz2", "xz", "rar", "7z"}

# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------------
def normalize_filename(name: str) -> str:
    name = unicodedata.normalize('NFKD', name)
    return name.replace(" ", "_")

def ensure_unique(destination: pathlib.Path) -> pathlib.Path:
    if not destination.exists():
        return destination
    base = destination.stem
    suffix = destination.suffix
    parent = destination.parent
    i = 1
    while True:
        candidate = parent / f"{base}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def category_for_extension(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    if ext in ARCHIVE_EXTS:
        return "Archives"
    return "Others"

def try_unpack_archive(src_path: pathlib.Path, dest_dir: pathlib.Path) -> bool:
    try:
        unpack_archive(str(src_path), str(dest_dir))
        return True
    except Exception as e:
        logging.warning(f"⚠️ Не удалось распаковать архив {src_path}: {e}")
        return False


def monitor(view):
    def wrapped(*args, **kwargs):
        started = time.perf_counter()
        HTTP_IN_PROGRESS.inc()
        status = 500
        try:
            result = view(*args, **kwargs)
            if isinstance(result, tuple) and len(result) >= 2:
                status = result[1]
            elif hasattr(result, "status_code"):
                status = result.status_code
            else:
                status = 200
            return result
        finally:
            path = request.path
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_DURATION.labels(request.method, path).observe(time.perf_counter() - started)
            HTTP_IN_PROGRESS.dec()

    wrapped.__name__ = view.__name__
    return wrapped

# ---------------------- ОСНОВНАЯ ЛОГИКА ----------------------
def organize_folder(base_path: pathlib.Path, dry_run=True, recursive=False, delete_archives=False):
    if not base_path.exists() or not base_path.is_dir():
        raise ValueError(f"Путь {base_path} не существует или не является директорией.")

    target_dirs = {cat: base_path / cat for cat in list(CATEGORIES.keys()) + ["Others"]}
    for d in target_dirs.values():
        if not dry_run:
            d.mkdir(exist_ok=True)

    items = [p for p in base_path.rglob("*") if p.is_file()] if recursive else [p for p in base_path.iterdir() if p.is_file()]

    # исключаем уже созданные целевые папки
    category_names = set(target_dirs.keys())
    filtered = []
    for p in items:
        relative_parts = [part for part in p.relative_to(base_path).parts]
        if any(part in category_names for part in relative_parts):
            continue
        filtered.append(p)

    for src in filtered:
        ext = src.suffix.lower().lstrip(".")
        cat = category_for_extension(ext)
        dest_folder = target_dirs.get(cat, base_path / "Others")

        # --- Архивы ---
        if cat == "Archives":
            archive_name = src.stem
            archive_dest = dest_folder / archive_name
            if dry_run:
                logging.info(f"[DRY-RUN] Архив {src} -> {archive_dest}")
                continue

            archive_dest.mkdir(parents=True, exist_ok=True)
            unpacked = try_unpack_archive(src, archive_dest)
            if unpacked:
                if delete_archives:
                    logging.info(f"✅ Распакован и удалён архив: {src} -> {archive_dest}")
                    src.unlink()
                else:
                    moved_archive = ensure_unique(archive_dest / src.name)
                    shutil.move(str(src), str(moved_archive))
                    logging.info(f"✅ Распакован и перемещён архив: {src} -> {moved_archive}")
            else:
                dest_path = ensure_unique(dest_folder / src.name)
                shutil.move(str(src), str(dest_path))
                logging.info(f"✅ Архив не распакован, просто перемещён: {src} -> {dest_path}")
        else:
            # --- Обычные файлы ---
            safe_name = normalize_filename(src.name)
            dest_path = ensure_unique(dest_folder / safe_name)
            if dry_run:
                logging.info(f"[DRY-RUN] {src} -> {dest_path}")
                continue

            dest_folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest_path))
            logging.info(f"✅ Перемещено: {src} -> {dest_path}")


@app.get("/")
@monitor
def status():
    return jsonify({"service": "file-organizer", "status": "ok"})


@app.get("/api/organize")
@monitor
def organize_api():
    """Виконує класифікацію імен файлів без зміни файлової системи."""
    names = request.args.getlist("file") or [
        "report.pdf",
        "photo.png",
        "archive.zip",
        "service.py",
        "unknown.data",
    ]
    result = {name: category_for_extension(pathlib.Path(name).suffix) for name in names}
    ORGANIZE_OPERATIONS.labels("success").inc()
    return jsonify({"files": result, "count": len(result)})


@app.get("/api/failure")
@monitor
def simulated_failure():
    ORGANIZE_OPERATIONS.labels("failure").inc()
    return jsonify({"error": "simulated organizer failure"}), 500


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

# ---------------------- НАСТРОЙКА ПАРАМЕТРОВ ----------------------
def parse_args():
    p = argparse.ArgumentParser(description="Организация папки 'Загрузки' по типам файлов.")
    p.add_argument("--path", "-p", type=str, default=None, help="Путь к папке (по умолчанию — Загрузки)")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Выполнить реальные действия")
    p.add_argument("--recursive", "-r", action="store_true", help="Сканировать вложенные папки")
    p.add_argument("--delete-archives", action="store_true", help="Удалять архивы после распаковки")
    p.add_argument("--serve", action="store_true", help="Запустить HTTP-сервис с Prometheus-метриками")
    p.add_argument("--port", type=int, default=8000, help="Порт HTTP-сервісу")
    return p.parse_args()

def default_downloads_path():
    home = pathlib.Path.home()
    for candidate in [home / "Downloads", home / "Загрузки"]:
        if candidate.exists():
            return candidate
    return home

def main():
    args = parse_args()
    if args.serve:
        app.run(host="0.0.0.0", port=args.port)
        return

    base = pathlib.Path(args.path) if args.path else default_downloads_path()

    logging.info(f"Организация файлов в: {base}")
    logging.info(f"Dry-run режим: {args.dry_run}")

    try:
        organize_folder(base, dry_run=args.dry_run, recursive=args.recursive, delete_archives=args.delete_archives)
        logging.info("=== ✅ Организация завершена ===")
        logging.info(f"Все операции записаны в: {log_file}")
    except Exception as e:
        logging.error(f"❌ Ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
