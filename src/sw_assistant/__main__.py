import multiprocessing

from .app import run


def main() -> int:
    multiprocessing.freeze_support()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
