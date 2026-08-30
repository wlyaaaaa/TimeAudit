# -*- coding: utf-8 -*-
"""Fail-closed contract shared by Grafana dashboard backup and restore."""

import glob
import os


RECOVERY_DATASOURCE_UID = "P7A9DAD60F8AB4C18"
RETIRED_DATASOURCE_UIDS = frozenset({"bfoc1vymtgni8a"})
POSTGRES_DATASOURCE_TYPES = frozenset(
    {"postgres", "grafana-postgresql-datasource"}
)


class DashboardContractError(ValueError):
    """A dashboard snapshot is unsafe or ambiguous for recovery."""


def _walk(value, path="dashboard"):
    yield value, path
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def validate_dashboard_document(dashboard, source="dashboard", expected_uid=None):
    """Validate a normalized dashboard before it enters backup or restore."""
    if not isinstance(dashboard, dict):
        raise DashboardContractError(f"{source}: dashboard root must be an object")

    uid = dashboard.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        raise DashboardContractError(f"{source}: dashboard uid is missing")
    if expected_uid is not None and uid != expected_uid:
        raise DashboardContractError(
            f"{source}: dashboard uid {uid!r} does not match {expected_uid!r}"
        )

    for node, path in _walk(dashboard):
        if isinstance(node, str) and node in RETIRED_DATASOURCE_UIDS:
            raise DashboardContractError(
                f"{source}: retired datasource uid at {path}"
            )
        if not isinstance(node, dict):
            continue

        transformations = node.get("transformations")
        if transformations is not None:
            if not isinstance(transformations, list):
                raise DashboardContractError(
                    f"{source}: transformations must be a list at {path}.transformations"
                )
            for index, transformation in enumerate(transformations):
                if not isinstance(transformation, dict):
                    raise DashboardContractError(
                        f"{source}: transformation must be an object at "
                        f"{path}.transformations[{index}]"
                    )
                transformation_id = transformation.get("id")
                if not isinstance(transformation_id, str) or not transformation_id.strip():
                    raise DashboardContractError(
                        f"{source}: transformation.id is missing at "
                        f"{path}.transformations[{index}].id"
                    )

        matcher = node.get("matcher")
        if isinstance(matcher, dict):
            matcher_id = matcher.get("id")
            if not isinstance(matcher_id, str) or not matcher_id.strip():
                raise DashboardContractError(
                    f"{source}: matcher.id is missing at {path}.matcher"
                )

        datasource_type = node.get("type")
        if datasource_type in POSTGRES_DATASOURCE_TYPES:
            datasource_uid = node.get("uid")
            if datasource_uid in RETIRED_DATASOURCE_UIDS:
                raise DashboardContractError(
                    f"{source}: retired datasource uid at {path}.uid"
                )
            if datasource_uid != RECOVERY_DATASOURCE_UID:
                raise DashboardContractError(
                    f"{source}: PostgreSQL datasource uid is not the recovery uid "
                    f"at {path}"
                )
    return dashboard


def discover_dashboard_files(dashboard_dir, requested_file=None):
    """Return only active ``*.json`` snapshots; never accept editor backups."""
    if requested_file:
        path = os.path.abspath(requested_file)
        if os.path.splitext(path)[1].lower() != ".json":
            raise DashboardContractError(
                "requested dashboard must have the exact .json extension"
            )
        if not os.path.isfile(path):
            raise DashboardContractError("requested dashboard file does not exist")
        return [path]
    return sorted(glob.glob(os.path.join(dashboard_dir, "*.json")))
