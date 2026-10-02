"""Which environment a process runs in: one answer for every reader (debt TD-52).

In K3s every Trade pod used to say ``stg``: the base manifests set ``BIFROST_ENV=stg``
and mount the config as ``config.stg.yaml`` in DEV and PROD too, the filename helper
knew only dev and prod, and the ops helper rejected stg. Health surfaces disagreed by
app, and code was right only through a fallback chain.

The order, most deliberate first:

1. ``ops.control_profile`` in the loaded YAML — set per env in each overlay.
2. ``BIFROST_OPS_CONTROL_PROFILE`` env.
3. ``BIFROST_ENV`` env.
4. The config file's name (``config.<env>.yaml``), for a local run.

YAML before env on purpose: an image can reach a pod before the manifest that
corrects its env does (the deliver pipeline restarts pods before the GitOps sync),
and the overlay's ``control_profile`` has been right in every env all along.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

ENV_PROFILES = ("dev", "stg", "prod")


def _profile(value: Any) -> Optional[str]:
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ENV_PROFILES:
            return s
    return None


def profile_from_config_path(resolved_path: Optional[str]) -> Optional[str]:
    """``dev`` / ``stg`` / ``prod`` when the file is named ``config.<env>.yaml``, else None."""
    if not resolved_path:
        return None
    name = Path(resolved_path).name
    if name.startswith("config.") and name.endswith(".yaml"):
        return _profile(name[len("config.") : -len(".yaml")])
    return None


def deployment_profile(config: Optional[Mapping[str, Any]], resolved_path: Optional[str] = None) -> Optional[str]:
    """The environment this process serves: ``dev``, ``stg``, ``prod`` or None."""
    ops = (config or {}).get("ops")
    if isinstance(ops, Mapping):
        found = _profile(ops.get("control_profile"))
        if found:
            return found
    for env_key in ("BIFROST_OPS_CONTROL_PROFILE", "BIFROST_ENV"):
        found = _profile(os.environ.get(env_key, ""))
        if found:
            return found
    return profile_from_config_path(resolved_path)
