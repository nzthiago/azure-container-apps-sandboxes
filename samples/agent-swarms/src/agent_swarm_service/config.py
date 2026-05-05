from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum
from typing import ClassVar

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, model_validator

from agent_swarm_service.orchestration.models import (
    CopilotRuntimeSettings,
    HumanReviewMode,
    ModelSelection,
    SwarmAgentSettings,
    SwarmOptions,
    SwarmPlanningSettings,
    SwarmSandboxSettings,
    validate_sandbox_selector_values,
)


class SettingsError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


class AppSettings(BaseModel):
    base_url: AnyHttpUrl
    run_token_lifetime_hours: int = 12


class AzureSettings(BaseModel):
    subscription_id: str
    resource_group: str
    location: str
    storage_account_url: str
    sandbox_group_name: str


class DurableStorageBackend(str, Enum):
    AZURE = "azure"
    MEMORY = "memory"


class SandboxExecutionBackend(str, Enum):
    ACA = "aca"


class GitHubPublishBackend(str, Enum):
    AUTO = "auto"
    GITHUB_API = "github-api"


class OrchestrationBackend(str, Enum):
    DTS = "dts"
    LOCAL = "local"


class DurableStorageSettings(BaseModel):
    backend: DurableStorageBackend = DurableStorageBackend.AZURE
    connection_string: SecretStr | None = None
    container_name: str = "swarm-runtime"
    coordinator_queue_name: str = "swarm-coordinator"
    lease_duration_seconds: int = 60
    queue_visibility_timeout_seconds: int = 30
    poll_interval_seconds: float = 1.0
    background_worker_enabled: bool = True


class DurableTaskSchedulerSettings(BaseModel):
    setting_name: ClassVar[str] = "DTS_CONNECTION_STRING"
    connection_string: SecretStr | None = None
    worker_enabled: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> "DurableTaskSchedulerSettings":
        mapping = {} if values is None else values
        configured = mapping.get(cls.setting_name)
        if configured is not None and str(configured).strip():
            return cls(connection_string=configured)
        return cls()


class OrchestrationSettings(BaseModel):
    backend: OrchestrationBackend = OrchestrationBackend.DTS


class RuntimeDefaultsSettings(BaseModel):
    max_fix_chain_depth: int = 3
    max_replans: int = 1
    planner_model: str = "gpt-4.1"
    worker_model: str = "gpt-4.1"
    reviewer_model: str = "gpt-4.1"
    copilot_runtime_provider: str = "github-copilot-sdk"
    copilot_token_environment_variable: str = "GH_TOKEN"
    copilot_api_base_url: str = "https://models.github.ai/inference"
    human_review_mode: HumanReviewMode = HumanReviewMode.NONE
    plan_review_timeout_hours: int = 24
    sandbox_cpu: str = "4"
    sandbox_memory: str = "16Gi"
    sandbox_backend: SandboxExecutionBackend = SandboxExecutionBackend.ACA
    github_publish_backend: GitHubPublishBackend = GitHubPublishBackend.AUTO
    sandbox_idle_timeout_seconds: int | None = 300
    keep_failed_sandboxes: bool = False
    sandbox_disk_id: str | None = None

    @model_validator(mode="after")
    def validate_sandbox_selectors(self) -> "RuntimeDefaultsSettings":
        validate_sandbox_selector_values(
            sandbox_disk_id=self.sandbox_disk_id,
            layer_name="runtime defaults",
        )
        return self

    def to_swarm_options(self) -> SwarmOptions:
        return SwarmOptions(
            max_fix_chain_depth=self.max_fix_chain_depth,
            max_replans=self.max_replans,
            planning=SwarmPlanningSettings(
                human_review_mode=self.human_review_mode,
                plan_review_timeout_hours=self.plan_review_timeout_hours,
            ),
            models=SwarmAgentSettings(
                planner=ModelSelection(model=self.planner_model),
                worker=ModelSelection(model=self.worker_model),
                reviewer=ModelSelection(model=self.reviewer_model),
            ),
            copilot_runtime=CopilotRuntimeSettings(
                provider=self.copilot_runtime_provider,
                token_environment_variable=self.copilot_token_environment_variable,
                api_base_url=self.copilot_api_base_url,
            ),
            sandbox=SwarmSandboxSettings(
                cpu=self.sandbox_cpu,
                memory=self.sandbox_memory,
                idle_timeout_in_seconds=self.sandbox_idle_timeout_seconds,
                keep_failed_sandboxes=self.keep_failed_sandboxes,
                sandbox_disk_id=self.sandbox_disk_id,
            ),
        )


class ServiceSettings(BaseModel):
    app: AppSettings
    azure: AzureSettings
    dts: DurableTaskSchedulerSettings = Field(default_factory=DurableTaskSchedulerSettings)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    storage: DurableStorageSettings = Field(default_factory=DurableStorageSettings)
    runtime: RuntimeDefaultsSettings = Field(default_factory=RuntimeDefaultsSettings)

    @classmethod
    def for_local_development(cls) -> "ServiceSettings":
        return cls.model_validate(
            {
                "app": {
                    "base_url": "http://127.0.0.1:8000",
                },
                "azure": {
                    "subscription_id": "00000000-0000-0000-0000-000000000000",
                    "resource_group": "local-development-rg",
                    "location": "local",
                    "storage_account_url": "https://localdevelopment.blob.core.windows.net/",
                    "sandbox_group_name": "local-development-sandbox-group",
                },
                "dts": {
                    "connection_string": "Endpoint=http://127.0.0.1:8080;Authentication=ManagedIdentity;TaskHub=local-development",
                    "worker_enabled": False,
                },
                "orchestration": {
                    "backend": OrchestrationBackend.LOCAL.value,
                },
                "storage": {
                    "backend": DurableStorageBackend.MEMORY.value,
                    "background_worker_enabled": False,
                },
                "runtime": {
                    "sandbox_disk_id": (
                        "/subscriptions/00000000-0000-0000-0000-000000000000/"
                        "resourceGroups/local-development-rg/providers/Microsoft.App/diskImages/"
                        "local-agent-swarm-runtime"
                    ),
                },
            }
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        allow_default: bool = False,
    ) -> "ServiceSettings":
        values = dict(os.environ if env is None else env)
        resolved = _resolve_runtime_env(values)
        required = {
            "SWARM_APP_BASE_URL": resolved.get("SWARM_APP_BASE_URL"),
            "AZURE_SUBSCRIPTION_ID": resolved.get("AZURE_SUBSCRIPTION_ID"),
            "AZURE_RESOURCE_GROUP": resolved.get("AZURE_RESOURCE_GROUP"),
            "AZURE_LOCATION": resolved.get("AZURE_LOCATION"),
            "SWARM_STORAGE_ACCOUNT_URL": resolved.get("SWARM_STORAGE_ACCOUNT_URL"),
            "SWARM_SANDBOX_GROUP_NAME": resolved.get("SWARM_SANDBOX_GROUP_NAME"),
        }
        missing = [name for name, value in required.items() if value is None or not str(value).strip()]
        if missing:
            if allow_default and len(missing) == len(required):
                return cls.for_local_development()
            joined = ", ".join(sorted(missing))
            raise SettingsError(f"Missing required settings: {joined}")

        validate_sandbox_selector_values(
            sandbox_disk_id=resolved.get("SWARM_SANDBOX_DISK_ID"),
            layer_name="service defaults",
        )

        return cls.model_validate(
            {
                "app": {
                    "base_url": resolved["SWARM_APP_BASE_URL"],
                    "run_token_lifetime_hours": resolved.get("SWARM_RUN_TOKEN_LIFETIME_HOURS", 12),
                },
                "azure": {
                    "subscription_id": resolved["AZURE_SUBSCRIPTION_ID"],
                    "resource_group": resolved["AZURE_RESOURCE_GROUP"],
                    "location": resolved["AZURE_LOCATION"],
                    "storage_account_url": resolved["SWARM_STORAGE_ACCOUNT_URL"],
                    "sandbox_group_name": resolved["SWARM_SANDBOX_GROUP_NAME"],
                },
                "dts": {
                    "connection_string": resolved.get("DTS_CONNECTION_STRING"),
                    "worker_enabled": resolved.get("SWARM_DTS_WORKER_ENABLED", True),
                },
                "orchestration": {
                    "backend": resolved.get("SWARM_ORCHESTRATION_BACKEND", OrchestrationBackend.DTS.value),
                },
                "storage": {
                    "backend": resolved.get("SWARM_STORAGE_BACKEND", DurableStorageBackend.AZURE.value),
                    "connection_string": resolved.get("SWARM_STORAGE_CONNECTION_STRING"),
                    "container_name": resolved.get("SWARM_STORAGE_CONTAINER_NAME", "swarm-runtime"),
                    "coordinator_queue_name": resolved.get("SWARM_COORDINATOR_QUEUE_NAME", "swarm-coordinator"),
                    "lease_duration_seconds": resolved.get("SWARM_RUN_LEASE_DURATION_SECONDS", 60),
                    "queue_visibility_timeout_seconds": resolved.get(
                        "SWARM_COORDINATOR_QUEUE_VISIBILITY_TIMEOUT_SECONDS",
                        30,
                    ),
                    "poll_interval_seconds": resolved.get("SWARM_COORDINATOR_POLL_INTERVAL_SECONDS", 1.0),
                    "background_worker_enabled": resolved.get("SWARM_BACKGROUND_WORKER_ENABLED", True),
                },
                "runtime": {
                    "max_fix_chain_depth": resolved.get("SWARM_MAX_FIX_CHAIN_DEPTH", 3),
                    "max_replans": resolved.get("SWARM_MAX_REPLANS", 1),
                    "planner_model": resolved.get("SWARM_PLANNER_MODEL", "gpt-4.1"),
                    "worker_model": resolved.get("SWARM_WORKER_MODEL", "gpt-4.1"),
                    "reviewer_model": resolved.get("SWARM_REVIEWER_MODEL", "gpt-4.1"),
                     "copilot_runtime_provider": resolved.get(
                         "SWARM_COPILOT_RUNTIME",
                         resolved.get(
                             "SWARM_COPILOT_RUNTIME_PROVIDER",
                             "github-copilot-sdk",
                         ),
                     ),
                     "copilot_token_environment_variable": resolved.get(
                         "SWARM_COPILOT_TOKEN_ENV_VAR",
                         resolved.get(
                             "SWARM_COPILOT_TOKEN_ENVIRONMENT_VARIABLE",
                             "GH_TOKEN",
                         ),
                     ),
                     "copilot_api_base_url": resolved.get(
                         "SWARM_COPILOT_API_BASE_URL",
                         "https://models.github.ai/inference",
                     ),
                     "human_review_mode": resolved.get("SWARM_HUMAN_REVIEW_MODE", HumanReviewMode.NONE.value),
                     "plan_review_timeout_hours": resolved.get("SWARM_PLAN_REVIEW_TIMEOUT_HOURS", 24),
                    "sandbox_cpu": resolved.get("SWARM_SANDBOX_CPU", "4"),
                    "sandbox_memory": resolved.get("SWARM_SANDBOX_MEMORY", "16Gi"),
                    "sandbox_backend": resolved.get("SWARM_SANDBOX_BACKEND", SandboxExecutionBackend.ACA.value),
                    "github_publish_backend": resolved.get(
                        "SWARM_GITHUB_PUBLISH_BACKEND",
                        GitHubPublishBackend.AUTO.value,
                    ),
                     "sandbox_idle_timeout_seconds": resolved.get("SWARM_SANDBOX_IDLE_TIMEOUT_SECONDS", 300),
                     "keep_failed_sandboxes": resolved.get("SWARM_KEEP_FAILED_SANDBOXES", False),
                     "sandbox_disk_id": resolved.get("SWARM_SANDBOX_DISK_ID"),
                },
            }
        )


def load_settings(*, allow_default: bool = False) -> ServiceSettings:
    return ServiceSettings.from_env(allow_default=allow_default)


_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "SWARM_APP_BASE_URL": ("SWARM_APP_BASE_URL", "containerAppUrl"),
    "DTS_CONNECTION_STRING": ("DTS_CONNECTION_STRING", "dtsConnectionString"),
    "SWARM_STORAGE_ACCOUNT_URL": ("SWARM_STORAGE_ACCOUNT_URL", "storageAccountBlobEndpoint"),
    "SWARM_SANDBOX_GROUP_NAME": ("SWARM_SANDBOX_GROUP_NAME", "sandboxGroupName"),
}


def _resolve_runtime_env(values: Mapping[str, str]) -> dict[str, str]:
    resolved = dict(values)
    lowercase_values = {key.lower(): value for key, value in values.items()}
    for canonical, candidates in _ENV_ALIASES.items():
        for candidate in candidates:
            value = values.get(candidate)
            if value is None:
                value = lowercase_values.get(candidate.lower())
            if value is not None and str(value).strip():
                resolved[canonical] = value
                break
    return resolved
