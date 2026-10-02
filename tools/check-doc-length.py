import sys
from pathlib import Path

MAX_LINES = 100


def main():
    errors = False
    for path in map(Path, sys.argv[1:]):
        n = len(path.read_text().splitlines())
        if n > MAX_LINES:
            sys.stderr.write(f"{path}: {n} lines, more than {MAX_LINES}\n")
            errors = True

    sys.exit(int(errors))


if __name__ == "__main__":
    main()
