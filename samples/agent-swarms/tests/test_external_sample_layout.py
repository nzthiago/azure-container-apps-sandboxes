from __future__ import annotations

import unittest

from contract_support import (
    REQUIRED_DOCS,
    REQUIRED_INFRA_FILES,
    REQUIRED_SANDBOX_MODULES,
    REQUIRED_SETTINGS,
    assert_paths_exist,
    read_text,
    repo_path,
)


class ExternalSampleLayoutTests(unittest.TestCase):
    def test_external_docs_stack_exists_and_describes_dts_storage_split(self) -> None:
        missing = [path for path in REQUIRED_DOCS if not repo_path(path).exists()]
        if missing:
            raise unittest.SkipTest(
                "Required documentation files are missing: " + ", ".join(missing)
            )

        readme_text = read_text("README.md")
        self.assertIn("azd up", readme_text)
        self.assertIn("azd down", readme_text)
        self.assertIn("GitHub", readme_text)
        self.assertIn("/health", readme_text)
        self.assertIn("DTS", readme_text)
        self.assertIn("Azure Storage", readme_text)
        self.assertIn("ACA Sandboxes", readme_text)

    def test_azure_manifest_targets_python_container_apps_flow(self) -> None:
        assert_paths_exist(
            self,
            ("azure.yaml", r"hooks\preprovision.ps1", r"hooks\preprovision.sh"),
            "External validation starts at the root azd manifest.",
        )

        manifest = read_text("azure.yaml")
        self.assertIn("provider: bicep", manifest)
        self.assertIn("path: ./infra", manifest)
        self.assertIn("language: python", manifest)
        self.assertIn("host: containerapp", manifest)
        self.assertIn("project: ./src/agent_swarm_service", manifest)
        self.assertIn("preprovision", manifest)
        self.assertNotIn("    module: main", manifest)
        self.assertIn("posix:", manifest)
        self.assertIn("windows:", manifest)
        self.assertIn("shell: sh", manifest)
        self.assertIn("shell: pwsh", manifest)
        self.assertIn("run: ./hooks/preprovision.sh", manifest)
        self.assertIn("run: ./hooks/preprovision.ps1", manifest)

        preprovision_ps1 = read_text(r"hooks\preprovision.ps1")
        preprovision_sh = read_text(r"hooks\preprovision.sh")
        self.assertIn("Microsoft.App", preprovision_ps1)
        self.assertIn("Microsoft.DurableTask", preprovision_ps1)
        self.assertIn("Microsoft.App", preprovision_sh)
        self.assertIn("Microsoft.DurableTask", preprovision_sh)

    def test_dockerfile_keeps_preview_wheels_repo_local_for_azd_packaging(self) -> None:
        assert_paths_exist(
            self,
            (
                "Dockerfile",
                r"vendor\wheels\azure_containerapps_sandbox-0.1.0b1-py3-none-any.whl",
            ),
            "The azd packaging path depends on a repo-root Dockerfile plus bundled preview SDK wheels.",
        )

        dockerfile = read_text("Dockerfile")
        pyproject = read_text("pyproject.toml")
        self.assertIn('"github-copilot-sdk==0.3.0"', pyproject)
        self.assertIn("apt-get install --no-install-recommends -y git ca-certificates", dockerfile)
        self.assertIn("COPY vendor/wheels /vendor-wheels", dockerfile)
        self.assertIn(
            "pip wheel --no-cache-dir --no-deps --wheel-dir /wheels --find-links=/vendor-wheels .",
            dockerfile,
        )
        self.assertIn("pip download --no-cache-dir --dest /wheels --find-links=/vendor-wheels", dockerfile)
        self.assertIn("/wheels/agent_swarm_service-*.whl", dockerfile)

    def test_infra_package_contains_dts_runtime_resources_and_expected_settings(self) -> None:
        assert_paths_exist(
            self,
            REQUIRED_INFRA_FILES,
            "The external-sample path requires a root azure.yaml plus an infra package.",
        )

        main_bicep = read_text(r"infra\main.bicep")
        main_json = read_text(r"infra\main.json")
        parameters = read_text(r"infra\main.parameters.json")

        for token in (
            "Microsoft.ContainerRegistry/registries",
            "Microsoft.App/managedEnvironments",
            "Microsoft.App/containerApps",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
            "Microsoft.Storage/storageAccounts",
            "Microsoft.App/sandboxGroups",
            "Microsoft.OperationalInsights/workspaces",
            "Microsoft.Insights/components",
            "Microsoft.DurableTask/schedulers",
            "Microsoft.DurableTask/schedulers/taskHubs",
            "DTS_CONNECTION_STRING",
            "SWARM_COPILOT_RUNTIME",
            "SWARM_COPILOT_AUTH_MODE",
            "SWARM_COPILOT_TOKEN_ENV_VAR",
            "SWARM_COPILOT_USE_LOGGED_IN_USER",
            "AZURE_CONTAINER_REGISTRY_ENDPOINT",
            "SWARM_SANDBOX_DISK_ID",
            "Durable Task Data Contributor",
            "/health",
        ):
            self.assertIn(token, main_bicep)

        for token in (
            "SWARM_COPILOT_RUNTIME",
            "SWARM_COPILOT_AUTH_MODE",
            "SWARM_COPILOT_TOKEN_ENV_VAR",
            "SWARM_COPILOT_USE_LOGGED_IN_USER",
            "SWARM_SANDBOX_DISK_ID",
        ):
            self.assertIn(token, main_json)

        for setting_name in (
            "SWARM_COPILOT_RUNTIME",
            "SWARM_COPILOT_AUTH_MODE",
            "SWARM_COPILOT_TOKEN_ENV_VAR",
            "SWARM_COPILOT_USE_LOGGED_IN_USER",
            "SWARM_SANDBOX_DISK_ID",
        ):
            self.assertIn(setting_name, parameters)

        for setting_name in REQUIRED_SETTINGS:
            self.assertIn(setting_name, main_bicep)
            self.assertIn(setting_name, parameters)

        self.assertIn(
            "output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer",
            main_bicep,
        )
        self.assertIn('"AZURE_CONTAINER_REGISTRY_ENDPOINT": {', main_json)
        self.assertIn(
            '"value": "[reference(resourceId(\'Microsoft.ContainerRegistry/registries\', variables(\'containerRegistryName\')), \'2023-07-01\').loginServer]"',
            main_json,
        )

    def test_sample_sandbox_image_dockerfile_matches_runtime_contract(self) -> None:
        assert_paths_exist(
            self,
            (
                r"sandbox-image\Dockerfile",
                r"sandbox-image\run-role.py",
            ),
            "The repo includes a reusable sandbox image sample for private DiskId workflows.",
        )

        dockerfile = read_text(r"sandbox-image\Dockerfile")
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn(
            "COPY src/agent_swarm_service/orchestration/copilot_runtime.py /opt/agent-swarm/copilot_runtime.py",
            dockerfile,
        )
        self.assertIn("COPY sandbox-image/run-role.py /opt/agent-swarm/run-role.py", dockerfile)
        self.assertIn("apt-get install --no-install-recommends -y git ca-certificates", dockerfile)
        self.assertIn("python -m pip install --no-cache-dir --upgrade pip", dockerfile)
        self.assertIn("python -m pip install --no-cache-dir github-copilot-sdk==0.3.0", dockerfile)
        self.assertIn("mkdir -p /workspace", dockerfile)
        self.assertIn('CMD ["/bin/sh"]', dockerfile)

    def test_sandbox_adapter_seam_files_exist(self) -> None:
        assert_paths_exist(
            self,
            REQUIRED_SANDBOX_MODULES,
            "ACA sandbox preview SDK churn is kept behind a typed adapter seam.",
        )


if __name__ == "__main__":
    unittest.main()
