"""Open a local codeplug and send its TYT Reader snapshot to standard output."""

import json
from pathlib import Path
import sys


def load_file(path):
    extension = path.suffix.lower()
    if extension == ".rdt":
        from import_rdt import import_rdt
        return import_rdt(path)
    if extension in (".yaml", ".yml"):
        from import_qdmr import import_qdmr
        return import_qdmr(path)
    raise ValueError("Поддерживаются файлы TYT CPS (.rdt) и qdmr (.yaml, .yml).")


def main():
    if len(sys.argv) != 2:
        print("Укажите один файл .rdt, .yaml или .yml.", file=sys.stderr)
        return 2
    try:
        report = load_file(Path(sys.argv[1]))
        data = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    except (OSError, ValueError) as error:
        print(str(error)[:500], file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
