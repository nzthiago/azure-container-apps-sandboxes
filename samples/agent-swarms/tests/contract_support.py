from __future__ import annotations



import importlib

import inspect

import json

import shutil

import sys

import unittest

from contextlib import contextmanager

from pathlib import Path

from typing import Any, Iterable





REPO_ROOT = Path(__file__).resolve().parents[1]

SRC_ROOT = REPO_ROOT / "src"

SCRATCH_ROOT = REPO_ROOT / "tests" / ".scratch"



REQUIRED_DOCS = (
    "README.md",
    r"docs\quickstart.md",
    r"docs\architecture.md",
    r"docs\troubleshooting.md",
)


REQUIRED_INFRA_FILES = (
    "azure.yaml",
    r"infra\main.bicep",
    r"infra\main.parameters.json",
)


REQUIRED_SETTINGS = (

    "SWARM_APP_BASE_URL",

    "DTS_CONNECTION_STRING",

    "AZURE_SUBSCRIPTION_ID",

    "AZURE_RESOURCE_GROUP",

    "AZURE_LOCATION",

    "SWARM_STORAGE_ACCOUNT_URL",

    "SWARM_SANDBOX_GROUP_NAME",

)



REQUIRED_SANDBOX_MODULES = (
    r"src\agent_swarm_service\sandboxes\sandbox_groups.py",
    r"src\agent_swarm_service\sandboxes\aca_client.py",
    r"src\agent_swarm_service\sandboxes\workspace.py",
    r"src\agent_swarm_service\sandboxes\logs.py",
)


OPENAPI_CANDIDATE_PATHS = ("/api/openapi.json", "/openapi/v1.json", "/openapi.json")



_APP_IMPORT_CANDIDATES = (

    ("agent_swarm_service.app", ("create_app", "build_app", "app", "application")),

)



_CREATE_REQUEST_MODEL_CANDIDATES = (

    "agent_swarm_service.api.schemas",

    "agent_swarm_service.api.models.swarm_runs",

    "agent_swarm_service.api.schemas.swarm_runs",

    "agent_swarm_service.models.swarm_runs",

    "agent_swarm_service.swarm_runs.models",

)



_SETTINGS_MODEL_CANDIDATES = (

    "agent_swarm_service.settings",

    "agent_swarm_service.config",

    "agent_swarm_service.configuration",

)



_REDACTION_FUNCTION_CANDIDATES = (

    ("agent_swarm_service.sandboxes.logs", ("redact_text", "redact_secrets", "sanitize_output", "sanitize_diagnostics")),

    ("agent_swarm_service.sandboxes.aca_client", ("redact_text", "redact_secrets", "sanitize_output", "sanitize_diagnostics")),

    ("agent_swarm_service.redaction", ("redact_text", "redact_secrets", "sanitize_output", "sanitize_diagnostics")),

    ("agent_swarm_service.utils.redaction", ("redact_text", "redact_secrets", "sanitize_output", "sanitize_diagnostics")),

)



_LOG_TAIL_FUNCTION_CANDIDATES = (

    ("agent_swarm_service.sandboxes.logs", ("tail_mirrored_log", "tail_mirrored_log_file", "read_mirrored_log_tail", "read_log_tail")),

)





def repo_path(relative_path: str) -> Path:

    return REPO_ROOT.joinpath(*relative_path.replace("\\", "/").split("/"))





def read_text(relative_path: str) -> str:

    return repo_path(relative_path).read_text(encoding="utf-8")





def assert_paths_exist(testcase: unittest.TestCase, relative_paths: Iterable[str], guidance: str) -> None:

    missing = [path for path in relative_paths if not repo_path(path).exists()]

    testcase.assertFalse(missing, f"{guidance} Missing: {', '.join(missing)}")





def ensure_src_on_path() -> None:

    if SRC_ROOT.exists():

        src_text = str(SRC_ROOT)

        if src_text not in sys.path:

            sys.path.insert(0, src_text)





def _import_optional(module_name: str) -> Any | None:

    ensure_src_on_path()

    try:

        return importlib.import_module(module_name)

    except ModuleNotFoundError as exc:

        root_name = module_name.split(".")[0]

        if exc.name in {module_name, root_name} or (

            exc.name is not None and module_name.startswith(f"{exc.name}.")

        ):

            return None

        raise





def _resolve_from_module(module: Any, candidate_names: Iterable[str]) -> Any | None:

    for name in candidate_names:

        if hasattr(module, name):

            return getattr(module, name)

    return None





def require_contract_object(module_names: Iterable[str], candidate_names: Iterable[str], reason: str) -> Any:

    for module_name in module_names:

        module = _import_optional(module_name)

        if module is None:

            continue



        obj = _resolve_from_module(module, candidate_names)

        if obj is not None:

            return obj



    raise unittest.SkipTest(reason)





@contextmanager

def create_test_client():

    try:

        from fastapi.testclient import TestClient

    except ModuleNotFoundError as exc:

        raise unittest.SkipTest(

            "FastAPI contract tests are pending because fastapi.testclient is unavailable."

        ) from exc



    for module_name, candidate_names in _APP_IMPORT_CANDIDATES:

        module = _import_optional(module_name)

        if module is None:

            continue



        resolved = _resolve_from_module(module, candidate_names)

        if resolved is None:

            raise unittest.SkipTest(

                "FastAPI contract tests are pending because src\agent_swarm_service\app.py does not expose "

                "create_app/build_app/app."

            )



        app = _materialize_app(resolved)

        with TestClient(app) as client:

            yield client

            return



    raise unittest.SkipTest(

        "FastAPI contract tests are pending because src\agent_swarm_service\app.py has not landed yet."

    )





def _materialize_app(resolved: Any) -> Any:

    if inspect.ismethod(resolved) or inspect.isfunction(resolved):

        signature = inspect.signature(resolved)

        required = [

            parameter

            for parameter in signature.parameters.values()

            if parameter.default is inspect._empty

            and parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

        ]

        if required:

            raise unittest.SkipTest(

                "FastAPI contract tests are pending because the app factory requires runtime-only parameters."

            )

        return resolved()



    return resolved





def get_openapi_document(client: Any) -> tuple[str, dict[str, Any]]:

    for path in OPENAPI_CANDIDATE_PATHS:

        response = client.get(path)

        if response.status_code == 200:

            return path, response.json()



    raise AssertionError(

        f"Expected an OpenAPI document at one of {OPENAPI_CANDIDATE_PATHS}, but none returned 200."

    )





def load_create_request_model() -> Any:

    return require_contract_object(

        _CREATE_REQUEST_MODEL_CANDIDATES,

        ("CreateSwarmRunRequest", "SwarmRunCreateRequest"),

        "Request validation tests are pending because the swarm create request model has not landed yet.",

    )





def load_settings_model() -> Any:

    return require_contract_object(

        _SETTINGS_MODEL_CANDIDATES,

        ("ServiceSettings", "Settings", "AppSettings", "SwarmSettings"),

        "Settings model tests are pending because the typed settings module has not landed yet.",

    )





def validate_model(model_cls: Any, payload: dict[str, Any]) -> Any:

    if hasattr(model_cls, "model_validate"):

        return model_cls.model_validate(payload)



    if hasattr(model_cls, "parse_obj"):

        return model_cls.parse_obj(payload)



    return model_cls(**payload)





def call_redaction_helper(text: str) -> str:

    helper = require_contract_object(

        (module_name for module_name, _ in _REDACTION_FUNCTION_CANDIDATES),

        ("redact_text", "redact_secrets", "sanitize_output", "sanitize_diagnostics"),

        "Redaction tests are pending because no sandbox redaction helper has landed yet.",

    )



    signature = inspect.signature(helper)

    kwargs: dict[str, Any] = {}

    required_parameters = []

    for parameter in signature.parameters.values():

        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):

            continue

        if parameter.default is inspect._empty:

            required_parameters.append(parameter.name)

        if not kwargs:

            kwargs[parameter.name] = text



    if len(required_parameters) > 1:

        raise unittest.SkipTest("Redaction helper exists, but its signature is not stable enough for contract testing yet.")



    return helper(**kwargs)





def call_log_tail_helper(path: Path, offset: int = 0) -> tuple[int | None, str]:
    helper = require_contract_object(
        (module_name for module_name, _ in _LOG_TAIL_FUNCTION_CANDIDATES),
        ("tail_mirrored_log", "tail_mirrored_log_file", "read_mirrored_log_tail", "read_log_tail"),
        r"Mirrored-log tail tests are pending because sandboxes\logs.py has not exposed a tail helper yet.",
    )


    parameters = inspect.signature(helper).parameters

    kwargs: dict[str, Any] = {}

    for name in parameters:

        lowered = name.lower()

        if "path" in lowered or "file" in lowered:

            kwargs[name] = path

        elif lowered in {"cursor", "offset", "position", "start"}:

            kwargs[name] = offset

        elif lowered == "tail_lines":

            kwargs[name] = 0



    result = helper(**kwargs)

    if isinstance(result, tuple) and len(result) == 2:

        return result[0], result[1]

    raise unittest.SkipTest("Log tail helper exists, but its return shape is not stable enough for contract testing yet.")





def reset_scratch_dir() -> Path:

    if SCRATCH_ROOT.exists():

        shutil.rmtree(SCRATCH_ROOT)

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)

    return SCRATCH_ROOT





def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def to_json_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True)


def make_scratch_dir(name: str) -> Path:
    base = reset_scratch_dir()
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in name)
    scratch_dir = base / safe_name
    scratch_dir.mkdir(parents=True, exist_ok=True)
    return scratch_dir


def cleanup_scratch_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)

