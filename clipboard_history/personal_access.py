"""Read the existing PCConfig shared lease; never issue a TimeAudit grant."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
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
        self._policy_injected = policy is not None
        self._policy_digest = None
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
        with self._lock:
            self._source = (path, boot["unix"])
        return self.status()

    def _current_policy(self):
        """Use the current installed policy without caching an access decision."""
        if self._policy_injected:
            return self._policy
        policy_path = BROKER.with_name("personal_environment.py")
        source = policy_path.read_bytes()
        digest = hashlib.sha256(source).digest()
        with self._lock:
            if digest == self._policy_digest and self._policy is not None:
                return self._policy
            # Compile the exact bytes observed at this trusted installed path.
            # This avoids a stale timestamp-based .pyc after an atomic release.
            module = types.ModuleType("_timeaudit_b2_policy")
            module.__file__ = str(policy_path)
            directory = str(BROKER.parent)
            sys.path.insert(0, directory)
            try:
                exec(compile(source, str(policy_path), "exec"), module.__dict__)
            finally:
                sys.path.remove(directory)
            if not callable(getattr(module, "public_status", None)):
                raise ValueError("personal_access_policy_invalid")
            self._policy = module
            self._policy_digest = digest
            return module

    def status(self) -> dict:
        """Tiny read on each UI check; no subprocess, grant cache or renewal."""
        with self._lock:
            source = self._source
        try:
            if source is None:
                raise ValueError("personal_access_not_initialized")
            path, boot = source
            policy = self._current_policy()
            state = json.loads(path.read_text(encoding="utf-8"))
            # A process cannot survive a Windows reboot. The canonical boot
            # observation from this viewer launch is stable for its lifetime.
            return policy.public_status(state, state_path=str(path),
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
