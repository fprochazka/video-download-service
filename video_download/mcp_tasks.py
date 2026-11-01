"""MCP tasks (the io.modelcontextprotocol/tasks extension, SEP-2663) for download_video, kept in metadata.json.

A client that declares the extension on its tools/call gets a task instead of the job: the task id is the job's id, and
tasks/get reads the job's metadata.json, so a task outlives a restart of the service and needs no task store of its own.
The task completes when the job does, with the same result get_job returns; a failed job completes the task with
isError, because the extension keeps the failed status for JSON-RPC errors. FastMCP's own TasksExtension
(fastmcp-tasks) keeps tasks in Docket, which loses them on a restart unless it runs on Redis.
"""

import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any

import mcp_types
import mcp_types.methods
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.extensions import MethodBinding, ServerExtension, read_client_extension_settings
from fastmcp.tools import ToolResult
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_NAME_HEADER, decode_header_value
from mcp_types import INVALID_PARAMS, RequestParams
from mcp_types.jsonrpc import HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ConfigDict, Field

from video_download import config, mcp_server, storage
from video_download.storage import load_metadata, update_metadata

logger = logging.getLogger(__name__)

# The tools that answer with a task for a client that declared the extension
TASK_TOOLS = frozenset({'download_video'})

POLL_INTERVAL_MS = 5000

CANCEL_NOT_SUPPORTED = 'cancellation was requested, but the service cannot stop a running job'


class TaskParams(RequestParams):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias='taskId')


class UpdateTaskParams(TaskParams):
    input_responses: dict[str, Any] = Field(default_factory=dict, alias='inputResponses')


def task_fields(metadata: dict) -> dict:
    """The fields every task shape has; ttlMs is null because a job's metadata is never deleted"""
    state, message = mcp_server.job_state(metadata)
    if metadata.get('task_cancel_requested_at') and state not in mcp_server.TERMINAL_STATES:
        message = f'{message}; {CANCEL_NOT_SUPPORTED}'
    return {
        'taskId': metadata['download_id'],
        'status': 'completed' if state in mcp_server.TERMINAL_STATES else 'working',
        'statusMessage': message,
        'createdAt': metadata['created_at'],
        'lastUpdatedAt': metadata['updated_at'],
        'ttlMs': None,
        'pollIntervalMs': POLL_INTERVAL_MS,
    }


def task_result(metadata: dict) -> dict:
    """The tasks/get answer: the task, and once the job is done the CallToolResult get_job would return"""
    task = task_fields(metadata) | {'resultType': 'complete'}
    if task['status'] == 'completed':
        download_dir = config.DOWNLOADS_DIR / metadata['download_id']
        failed = mcp_server.job_state(metadata)[0] == 'failed'
        result = mcp_server.job_result(download_dir, metadata, mcp_server.request_base_url(), is_error=failed)
        call_result = mcp_types.CallToolResult(
            content=result.content, structured_content=result.structured_content, is_error=result.is_error
        )
        task['result'] = call_result.model_dump(by_alias=True, mode='json', exclude_none=True)
    return task


# --- Producing a task as the tools/call result

# The SDK validates every tools/call result of the 2026-07-28 protocol against CallToolResult | InputRequiredResult,
# which strips a task down to nothing; it has no API yet for an extension's own result shape. fastmcp-tasks wraps the
# SDK's serializer the same way, see fastmcp_tasks/wire_production.py. A task result passes through unchanged.
_original_serialize = None


def _serialize_server_result(method: str, version: str, data: Mapping[str, Any], **kwargs) -> dict[str, Any]:
    if (
        not kwargs
        and method == 'tools/call'
        and version in MODERN_PROTOCOL_VERSIONS
        and isinstance(data, Mapping)
        and data.get('resultType') == 'task'
    ):
        return dict(data)
    return _original_serialize(method, version, data, **kwargs)


@contextlib.contextmanager
def task_results_on_the_wire():
    global _original_serialize
    if _original_serialize is not None:
        yield
        return
    _original_serialize = mcp_types.methods.serialize_server_result
    mcp_types.methods.serialize_server_result = _serialize_server_result
    try:
        yield
    finally:
        mcp_types.methods.serialize_server_result = _original_serialize
        _original_serialize = None


class JobTasksExtension(ServerExtension):
    identifier = TASKS_EXTENSION_ID

    def methods(self) -> Sequence[MethodBinding]:
        versions = frozenset(MODERN_PROTOCOL_VERSIONS)
        return [
            MethodBinding('tasks/get', TaskParams, self.handle_get, versions),
            MethodBinding('tasks/update', UpdateTaskParams, self.handle_update, versions),
            MethodBinding('tasks/cancel', TaskParams, self.handle_cancel, versions),
        ]

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        with task_results_on_the_wire():
            yield

    async def intercept_tool_call(self, params, context, call_next):
        request_context = context.request_context
        opted_in = (
            params.name in TASK_TOOLS
            and request_context is not None
            and request_context.protocol_version in MODERN_PROTOCOL_VERSIONS
            and context.client_extension_settings(TASKS_EXTENSION_ID) is not None
        )
        result = await call_next()
        # The tool already created the job, so a tasks/get for its id resolves before the client sees the task
        if not opted_in or not isinstance(result, ToolResult) or result.is_error:
            return result
        metadata = load_metadata(config.DOWNLOADS_DIR / result.structured_content['download_id'])
        return task_fields(metadata) | {'resultType': 'task'}

    def task_metadata(self, ctx, task_id: str) -> dict:
        """The metadata of the task's job, after the checks every tasks/* request gets"""
        if read_client_extension_settings(ctx, TASKS_EXTENSION_ID) is None:
            raise MCPError(
                code=MISSING_REQUIRED_CLIENT_CAPABILITY,
                message=f'The client did not declare the {TASKS_EXTENSION_ID} extension for this request.',
                data={'requiredCapabilities': {'extensions': {TASKS_EXTENSION_ID: {}}}},
            )
        with contextlib.suppress(RuntimeError):
            header = get_http_request().headers.get(MCP_NAME_HEADER)
            if header is not None and decode_header_value(header) != task_id:
                raise MCPError(code=HEADER_MISMATCH, message=f"{MCP_NAME_HEADER} does not match the 'taskId'")
        try:
            download_dir = config.DOWNLOADS_DIR / str(uuid.UUID(task_id))
        except ValueError:
            download_dir = None
        metadata = load_metadata(download_dir) if download_dir else None
        if metadata is None:
            raise MCPError(code=INVALID_PARAMS, message=f'Failed to retrieve task: task {task_id!r} not found')
        return metadata

    async def handle_get(self, ctx, params: TaskParams) -> dict:
        return task_result(self.task_metadata(ctx, params.task_id))

    async def handle_update(self, ctx, params: UpdateTaskParams) -> dict:
        # A job never asks for input, there is never an outstanding request to answer
        self.task_metadata(ctx, params.task_id)
        return {'resultType': 'complete'}

    async def handle_cancel(self, ctx, params: TaskParams) -> dict:
        metadata = self.task_metadata(ctx, params.task_id)
        download_dir = config.DOWNLOADS_DIR / metadata['download_id']
        with storage.artifacts_lock:
            update_metadata(download_dir, task_cancel_requested_at=storage.now_iso())
        logger.info('Download %s: the MCP client asked to cancel its task, the job keeps running', params.task_id)
        return {'resultType': 'complete'}
