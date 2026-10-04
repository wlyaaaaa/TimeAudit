"""Rebuild the two website mobile dashboards from the six-panel canonical."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from grafana_dashboard_contract import (
    DashboardContractError,
    validate_dashboard_document,
)


DASHBOARD_DIR = Path(__file__).resolve().parent / "grafana_provisioning" / "dashboards"
CANONICAL_UID = "website-computer-status"
GROUPS = {
    "cpu-gpu": ((1, 2, 3, 4), "处理器/显卡"),
    "memory-network": ((5, 6), "内存/网络"),
}


def derive_website_dashboards(canonical):
    """Copy all query/style settings; change only identity, title and layout."""
    validate_dashboard_document(canonical, expected_uid=CANONICAL_UID)
    panels = canonical.get("panels")
    if (
        not isinstance(panels, list)
        or any(not isinstance(panel, dict) for panel in panels)
        or any(type(panel.get("id")) is not int for panel in panels)
        or [panel["id"] for panel in panels] != [1, 2, 3, 4, 5, 6]
    ):
        raise DashboardContractError("canonical must contain ordered panel IDs 1–6")
    for panel in panels:
        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets or any(
            not isinstance(target, dict)
            or not isinstance(target.get("rawSql"), str)
            or not target["rawSql"].strip()
            for target in targets
        ):
            raise DashboardContractError(
                f"canonical panel {panel['id']} requires complete targets/rawSql"
            )

    by_id = {panel["id"]: panel for panel in panels}
    outputs = {}
    for group, (panel_ids, label) in GROUPS.items():
        dashboard = deepcopy(canonical)
        dashboard.pop("id", None)
        dashboard["uid"] = f"{CANONICAL_UID}-{group}"
        dashboard["title"] = f"{canonical['title']} · {label}"
        dashboard["panels"] = [deepcopy(by_id[panel_id]) for panel_id in panel_ids]
        for index, panel in enumerate(dashboard["panels"]):
            panel["gridPos"] = {"h": 7, "w": 24, "x": 0, "y": index * 7}
        validate_dashboard_document(dashboard, expected_uid=dashboard["uid"])
        outputs[dashboard["uid"]] = dashboard
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report stale/missing outputs without writing"
    )
    args = parser.parse_args(argv)
    try:
        source = DASHBOARD_DIR / f"{CANONICAL_UID}.json"
        canonical = json.loads(source.read_text(encoding="utf-8"))
        dashboards = derive_website_dashboards(canonical)
        stale = []
        for uid, dashboard in dashboards.items():
            path = DASHBOARD_DIR / f"{uid}.json"
            expected = json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n"
            actual = path.read_text(encoding="utf-8") if path.exists() else None
            if actual != expected:
                stale.append(path.name)
                if not args.check:
                    path.write_text(expected, encoding="utf-8", newline="\n")
        if args.check and stale:
            print("Stale website dashboards: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("Website dashboards: " + ("updated " + ", ".join(stale) if stale else "current"))
        return 0
    except (OSError, ValueError) as error:
        print(f"Website dashboard generation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
