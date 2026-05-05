from __future__ import annotations

import unittest

from contract_support import REQUIRED_SETTINGS, load_create_request_model, load_settings_model, validate_model
from agent_swarm_service.api.schemas import CreateSwarmRunRequest
from agent_swarm_service.config import ServiceSettings


EXPECTED_SETTING_FIELDS = {
    "SWARM_APP_BASE_URL": "app.base_url",
    "DTS_CONNECTION_STRING": "dts.connection_string",
    "AZURE_SUBSCRIPTION_ID": "azure.subscription_id",
    "AZURE_RESOURCE_GROUP": "azure.resource_group",
    "AZURE_LOCATION": "azure.location",
    "SWARM_STORAGE_ACCOUNT_URL": "azure.storage_account_url",
    "SWARM_SANDBOX_GROUP_NAME": "azure.sandbox_group_name",
}


def _collect_field_paths(model_cls: type, prefix: str = "") -> set[str]:
    fields = getattr(model_cls, "model_fields", {})
    discovered: set[str] = set()
    for name, field_info in fields.items():
        path = f"{prefix}.{name}" if prefix else name
        discovered.add(path)
        annotation = getattr(field_info, "annotation", None)
        nested_fields = getattr(annotation, "model_fields", None)
        if nested_fields:
            discovered.update(_collect_field_paths(annotation, path))
    return discovered


class RequestValidationContractTests(unittest.TestCase):
    def test_create_request_requires_non_blank_github_pat(self) -> None:
        model_cls = load_create_request_model()

        payloads = (
            {
                "prompt": "Implement task 9",
                "repositoryUrl": "https://github.com/octo/repo",
                "githubPat": "   ",
            },
            {
                "prompt": "Implement task 9",
                "repository_url": "https://github.com/octo/repo",
                "github_pat": "",
            },
        )

        for payload in payloads:
            try:
                validate_model(model_cls, payload)
            except Exception as exc:  # noqa: BLE001
                self.assertIn("github", str(exc).lower())
                self.assertIn("required", str(exc).lower())
                return

        self.fail("Expected missing GitHub PAT content to fail validation, but model accepted blank tokens.")

    def test_create_request_rejects_dropped_options_fields(self) -> None:
        model_cls = load_create_request_model()

        # The simplified sample only accepts a small set of options. Extra
        # fields (including ones we used to accept like *_reasoning_effort,
        # max_concurrent_sandboxes, max_execution_rounds, etc.) must be
        # rejected so user typos surface immediately rather than being
        # silently dropped.
        for unsupported_field in (
            "plannerReasoningEffort",
            "workerReasoningEffort",
            "reviewerReasoningEffort",
            "maxExecutionRounds",
            "maxFixChainDepth",
            "maxReplans",
            "maxConcurrentSandboxes",
            "maxExecutionTimeoutMinutes",
            "keepFailedSandboxes",
            "idleTimeoutInSeconds",
            "totallyMadeUpField",
        ):
            payload = {
                "prompt": "Implement task 9",
                "repositoryUrl": "https://github.com/octo/repo",
                "githubPat": "ghp_example",
                "options": {unsupported_field: "anything"},
            }
            with self.subTest(field=unsupported_field):
                with self.assertRaises(Exception) as ctx:
                    validate_model(model_cls, payload)
                message = str(ctx.exception).lower()
                self.assertTrue(
                    "extra" in message or "not permitted" in message or "forbidden" in message,
                    msg=f"Unexpected error for {unsupported_field}: {ctx.exception!r}",
                )

    def test_create_request_rejects_unknown_human_review_mode(self) -> None:
        model_cls = load_create_request_model()

        payloads = (
            {
                "prompt": "Implement task 9",
                "repositoryUrl": "https://github.com/octo/repo",
                "githubPat": "ghp_example",
                "options": {"humanReviewMode": "Optional"},
            },
            {
                "prompt": "Implement task 9",
                "repository_url": "https://github.com/octo/repo",
                "github_pat": "ghp_example",
                "options": {"human_review_mode": "Optional"},
            },
        )

        for payload in payloads:
            try:
                validate_model(model_cls, payload)
            except Exception as exc:  # noqa: BLE001
                message = str(exc).lower()
                self.assertTrue(
                    "human" in message or "review" in message or "enum" in message,
                    msg=f"Unexpected validation error: {exc!r}",
                )
                return

        self.fail("Expected unsupported human review mode to fail validation, but model accepted 'Optional'.")

    def test_settings_model_carries_reviewed_required_config_fields(self) -> None:
        settings_cls = load_settings_model()
        field_paths = _collect_field_paths(settings_cls)
        field_blob = " ".join(sorted(field_paths)).lower()
        for required in REQUIRED_SETTINGS:
            self.assertIn(EXPECTED_SETTING_FIELDS[required], field_blob)

    def test_settings_loader_accepts_azd_output_aliases_for_runtime_startup(self) -> None:
        settings = ServiceSettings.from_env(
            {
                "CONTAINERAPPURL": "https://swarm.example.com",
                "DTSCONNECTIONSTRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
                "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_RESOURCE_GROUP": "rg-swarm",
                "AZURE_LOCATION": "westus2",
                "STORAGEACCOUNTBLOBENDPOINT": "https://storage.blob.core.windows.net/",
                "SANDBOXGROUPNAME": "swarm-sandbox-group",
            }
        )

        self.assertEqual(str(settings.app.base_url), "https://swarm.example.com/")
        self.assertEqual(settings.runtime.copilot_token_environment_variable, "GH_TOKEN")
        self.assertEqual(
            settings.dts.connection_string.get_secret_value(),
            "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
        )
        self.assertEqual(settings.azure.storage_account_url, "https://storage.blob.core.windows.net/")
        self.assertEqual(settings.azure.sandbox_group_name, "swarm-sandbox-group")

    def test_settings_loader_reads_swarm_copilot_token_env_var_alias(self) -> None:
        settings = ServiceSettings.from_env(
            {
                "SWARM_APP_BASE_URL": "https://swarm.example.com",
                "DTS_CONNECTION_STRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
                "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_RESOURCE_GROUP": "rg-swarm",
                "AZURE_LOCATION": "westus2",
                "SWARM_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net/",
                "SWARM_SANDBOX_GROUP_NAME": "swarm-sandbox-group",
                "SWARM_COPILOT_TOKEN_ENV_VAR": "GH_TOKEN",
            }
        )

        self.assertEqual(settings.runtime.copilot_token_environment_variable, "GH_TOKEN")

    def test_settings_loader_reads_private_sandbox_disk_id_default(self) -> None:
        settings = ServiceSettings.from_env(
            {
                "SWARM_APP_BASE_URL": "https://swarm.example.com",
                "DTS_CONNECTION_STRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
                "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_RESOURCE_GROUP": "rg-swarm",
                "AZURE_LOCATION": "westus2",
                "SWARM_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net/",
                "SWARM_SANDBOX_GROUP_NAME": "swarm-sandbox-group",
                "SWARM_SANDBOX_DISK_ID": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image",
            }
        )

        sandbox = settings.runtime.to_swarm_options().sandbox

        self.assertEqual(
            sandbox.sandbox_disk_id,
            "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image",
        )
        self.assertEqual(
            sandbox.create_sandbox_selector_kwargs(),
            {"disk_id": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image"},
        )

    def test_settings_loader_rejects_retired_fake_runtime_backends(self) -> None:
        env = {
            "SWARM_APP_BASE_URL": "https://swarm.example.com",
            "DTS_CONNECTION_STRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
            "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
            "AZURE_RESOURCE_GROUP": "rg-swarm",
            "AZURE_LOCATION": "westus2",
            "SWARM_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net/",
            "SWARM_SANDBOX_GROUP_NAME": "swarm-sandbox-group",
        }

        for field, value in (
            ("SWARM_SANDBOX_BACKEND", "fake"),
            ("SWARM_GITHUB_PUBLISH_BACKEND", "fake"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(Exception) as exc:
                    ServiceSettings.from_env({**env, field: value})
                self.assertIn(field.removeprefix("SWARM_").split("_")[0].lower(), str(exc.exception).lower())

    def test_create_request_sandbox_override_beats_service_default(self) -> None:
        settings = ServiceSettings.from_env(
            {
                "SWARM_APP_BASE_URL": "https://swarm.example.com",
                "DTS_CONNECTION_STRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
                "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_RESOURCE_GROUP": "rg-swarm",
                "AZURE_LOCATION": "westus2",
                "SWARM_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net/",
                "SWARM_SANDBOX_GROUP_NAME": "swarm-sandbox-group",
                "SWARM_SANDBOX_DISK_ID": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/default-image",
            }
        )
        request = CreateSwarmRunRequest.model_validate(
            {
                "prompt": "Implement task 9",
                "repositoryUrl": "https://github.com/octo/repo",
                "githubPat": "ghp_example",
                "options": {
                    "sandboxDiskId": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/request-image",
                },
            }
        )

        resolved = request.options.apply_to(settings.runtime.to_swarm_options())

        self.assertEqual(
            resolved.sandbox.sandbox_disk_id,
            "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/request-image",
        )
        self.assertEqual(
            resolved.sandbox.create_sandbox_selector_kwargs(),
            {"disk_id": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/request-image"},
        )


if __name__ == "__main__":
    unittest.main()
