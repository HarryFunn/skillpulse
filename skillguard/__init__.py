"""SkillGuard: version management and degradation detection for agent skill libraries."""

__version__ = "0.1.0"

from .models import Skill, SkillVersion, ExecutionRecord, HealthReport, SkillState
from .store import SkillStore
from .health import HealthChecker, HealthConfig
from .lifecycle import LifecycleManager
from .attribution import Attributor, AttributionConfig, AttributionReport, Cause
from .ingest import SessionIngestor

__all__ = [
    "Skill",
    "SkillVersion",
    "ExecutionRecord",
    "HealthReport",
    "SkillState",
    "SkillStore",
    "HealthChecker",
    "HealthConfig",
    "LifecycleManager",
    "Attributor",
    "AttributionConfig",
    "AttributionReport",
    "Cause",
    "SessionIngestor",
]
