"""Compatibility entry: use the shared read-only health contract."""
from timeaudit_health import main

if __name__ == "__main__":
    raise SystemExit(main())
