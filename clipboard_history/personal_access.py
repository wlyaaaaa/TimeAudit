"""Read the existing PCConfig shared lease; never issue a TimeAudit grant."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid


BROKER = Path(r"C:\ProgramData\PCConfig\AuthorityHost\tools\Invoke-SecretBroker.ps1")
STATUS_SCHEMA = "pcconfig.personal-environment-status.v3"


def permitted(status: dict) -> bool:
    access = status.get("privacy_access") if isinstance(status, dict) else None
    return (isinstance(access, dict) and status.get("schema") == STATUS_SCHEMA and status.get("status") == "pass"
            and status.get("data_state") == "unlocked"
            and access.get("status") == "pass" and type(access.get("expires_at_unix")) is int
            and access["expires_at_unix"] > 0)


class SharedPersonalAccess:
    def __init__(self, *, runner=None, policy=None):
        self._runner = runner or self._run
        self._policy = policy
        self._source = None
        self._lock = threading.Lock()
        self._caller = "timeaudit-clipboard-" + uuid.uuid4().hex

    def _run(self, action: str) -> dict:
        command = ["pwsh", "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass", "-File",
                   str(BROKER), "-Action", action, "-RootTaskId", self._caller, "-Json"]
        if action == "VerifyPersonalEnvironment":
            command.extend(["-PromptId", uuid.uuid4().hex])
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=360 if action == "VerifyPersonalEnvironment" else 30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(value, dict):
            raise ValueError("personal_access_status_unavailable")
        return value

    def initialize(self) -> dict:
        """One canonical call resolves the state and current boot, off the UI thread."""
        with self._lock:
            self._source = None
        status = self._runner("StatusPersonalEnvironment")
        boot = status.get("boot_time", {})
        if (status.get("schema") != STATUS_SCHEMA or status.get("status") != "pass"
                or boot.get("available") is not True or type(boot.get("unix")) is not int
                or boot["unix"] <= 0 or not isinstance(status.get("state_path"), str)):
            raise ValueError("personal_access_status_unavailable")
        path = Path(status["state_path"])
        if not path.is_absolute():
            raise ValueError("personal_access_state_path_invalid")
        if self._policy is None:
            # Reuse the installed owner's pure B2 decision code, including its
            # monotonic deadline and required-view checks. No copied policy.
            directory = str(BROKER.parent)
            sys.path.insert(0, directory)
            try:
                spec = importlib.util.spec_from_file_location(
                    "_timeaudit_b2_policy", BROKER.with_name("personal_environment.py"))
                policy = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(policy)
                self._policy = policy
            finally:
                sys.path.remove(directory)
        with self._lock:
            self._source = (path, boot["unix"])
        return self.status()

    def status(self) -> dict:
        """Tiny read on each UI check; no subprocess, grant cache or renewal."""
        with self._lock:
            source = self._source
        try:
            if source is None:
                raise ValueError("personal_access_not_initialized")
            path, boot = source
            state = json.loads(path.read_text(encoding="utf-8"))
            # A process cannot survive a Windows reboot. The canonical boot
            # observation from this viewer launch is stable for its lifetime.
            return self._policy.public_status(state, state_path=str(path),
                boot_unix=boot, boot_time_available=True, privacy_level="factor")
        except Exception:
            return {"schema": STATUS_SCHEMA, "status": "blocked", "data_state": "unknown"}

    def unlock(self) -> dict:
        if permitted(self.status()):
            return self.status()  # Existing shared period keeps its exact end.
        result = self._runner("VerifyPersonalEnvironment")
        # A factor/cancellation receipt is not a grant. Re-read the one shared
        # state regardless; never lock, retry, or switch factor on cancellation.
        status = self.initialize()
        return {"verification": result, "shared_status": status}
