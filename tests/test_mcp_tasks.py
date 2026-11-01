import json

import jsonschema
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp_tasks import call_tool_task
from fastmcp_tasks.client_models import (
    CancelTaskRequest,
    CancelTaskRequestParams,
    ClientGetTaskResult,
    GetTaskRequest,
    GetTaskRequestParams,
)
from mcp.shared.exceptions import MCPError
from mcp_types import Result

from tests.test_mcp import BASE_URL, MEDIA, VIDEO_URL, http_client, jobs_done, mcp_client, running_app, video
from tests.ytdlp_fakes import FakeYoutubeDL, raise_like_ytdlp
from video_download import mcp_tasks

JOB_ID = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'


async def get_task(client, task_id: str) -> ClientGetTaskResult:
    return await client.session.send_request(
        GetTaskRequest(params=GetTaskRequestParams(task_id=task_id)), ClientGetTaskResult
    )


def tasks_client():
    return mcp_client(client_class=Client)


@pytest.mark.anyio
async def test_download_video_answers_a_tasks_client_with_a_task_that_completes_with_the_job(
    downloads_dir, public_base_url
):
    FakeYoutubeDL.behaviour = video

    async with running_app(), tasks_client() as client:
        task = await call_tool_task(client, 'download_video', {'url': VIDEO_URL})
        created = task.create_result
        assert (created.status, created.poll_interval_ms, created.ttl_ms) == ('working', 5000, None)
        assert created.status_message in ('queued', 'downloading')
        await jobs_done()
        status = await task.status()
        result = await task.result()
        schema = {tool.name: tool.output_schema for tool in await client.list_tools()}['download_video']

    # The task id is the job id, so the job page and get_job work with it too
    assert task.task_id == result.structured_content['download_id']
    assert status.status == 'completed'
    assert status.status_message == 'completed'
    assert result.is_error is False
    assert result.structured_content['state'] == 'completed'
    assert result.structured_content['files'][0]['url'] == f'{BASE_URL}/downloads/{task.task_id}/{MEDIA}'
    jsonschema.validate(result.structured_content, schema)


@pytest.mark.anyio
async def test_a_tasks_client_gets_the_finished_job_from_call_tool(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), tasks_client() as client:
        # FastMCP's client polls the task to its end, as Claude Code does for a task in the foreground
        result = await client.call_tool('download_video', {'url': VIDEO_URL}, timeout=10)

    assert result.structured_content['state'] == 'completed'


@pytest.mark.anyio
async def test_the_task_of_a_failed_download_completes_with_an_error_result(downloads_dir):
    FakeYoutubeDL.behaviour = lambda opts, url: raise_like_ytdlp(Exception('Video unavailable'))

    async with running_app(), tasks_client() as client:
        task = await call_tool_task(client, 'download_video', {'url': VIDEO_URL}, raise_on_error=False)
        await jobs_done()
        status = await get_task(client, task.task_id)

    assert status.status == 'completed'
    assert status.result['isError'] is True
    assert status.result['structuredContent']['state'] == 'failed'
    assert status.result['structuredContent']['error_code'] == 'unavailable'
    assert status.status_message.startswith('failed (unavailable): ')


@pytest.mark.anyio
async def test_tasks_survive_a_restart_because_they_are_the_jobs_metadata(downloads_dir):
    FakeYoutubeDL.behaviour = video
    async with running_app(), tasks_client() as client:
        finished = await call_tool_task(client, 'download_video', {'url': VIDEO_URL})
        await jobs_done()
    # A job the restart interrupted
    (downloads_dir / JOB_ID).mkdir()
    metadata = {'download_id': JOB_ID, 'url': VIDEO_URL, 'status': 'downloading', 'files': []}
    metadata |= {'created_at': '2026-11-01T10:00:00+00:00', 'updated_at': '2026-11-01T10:00:00+00:00'}
    (downloads_dir / JOB_ID / 'metadata.json').write_text(json.dumps(metadata))

    async with running_app(), tasks_client() as client:
        completed = await get_task(client, finished.task_id)
        interrupted = await get_task(client, JOB_ID)

    assert completed.status == 'completed'
    assert completed.result['structuredContent']['state'] == 'completed'
    assert interrupted.status == 'completed'
    assert interrupted.result['isError'] is True
    assert interrupted.result['structuredContent']['error_code'] == 'interrupted'


@pytest.mark.anyio
async def test_cancel_is_acknowledged_and_the_job_keeps_running(downloads_dir):
    (downloads_dir / JOB_ID).mkdir(parents=True)
    metadata = {'download_id': JOB_ID, 'url': VIDEO_URL, 'status': 'downloading', 'files': []}
    metadata |= {'created_at': '2026-11-01T10:00:00+00:00', 'updated_at': '2026-11-01T10:00:00+00:00'}

    async with running_app(), tasks_client() as client:
        (downloads_dir / JOB_ID / 'metadata.json').write_text(json.dumps(metadata))
        await client.session.send_request(CancelTaskRequest(params=CancelTaskRequestParams(task_id=JOB_ID)), Result)
        status = await get_task(client, JOB_ID)

    assert status.status == 'working'
    assert status.status_message == f'downloading; {mcp_tasks.CANCEL_NOT_SUPPORTED}'


@pytest.mark.anyio
async def test_an_unknown_task_is_invalid_params(downloads_dir):
    async with running_app(), tasks_client() as client:
        with pytest.raises(MCPError) as unknown:
            await get_task(client, JOB_ID)
        with pytest.raises(MCPError) as malformed:
            await get_task(client, 'not-a-job')

    assert unknown.value.error.code == malformed.value.error.code == -32602


@pytest.mark.anyio
async def test_task_methods_need_the_extension_on_the_request(downloads_dir):
    async with running_app(), mcp_client() as client:
        with pytest.raises(MCPError) as error:
            await get_task(client, JOB_ID)

    assert error.value.error.code == -32021


@pytest.mark.anyio
async def test_a_rejected_url_is_a_tool_error_and_not_a_task(downloads_dir):
    async with running_app(), tasks_client() as client:
        with pytest.raises(ToolError, match='Playlists, channels, profiles and other collections are not supported'):
            await client.call_tool('download_video', {'url': 'https://www.youtube.com/playlist?list=PLx0s'})

    assert list(downloads_dir.iterdir()) == []


@pytest.mark.anyio
async def test_get_job_is_never_a_task(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), tasks_client() as client:
        task = await call_tool_task(client, 'download_video', {'url': VIDEO_URL})
        await jobs_done()
        with pytest.raises(ToolError, match='did not run as a task'):
            await call_tool_task(client, 'get_job', {'download_id': task.task_id})


@pytest.mark.anyio
async def test_the_task_on_the_wire_has_every_field_claude_code_requires(downloads_dir):
    FakeYoutubeDL.behaviour = video
    meta = {
        'io.modelcontextprotocol/protocolVersion': '2026-07-28',
        'io.modelcontextprotocol/clientInfo': {'name': 'raw', 'version': '1'},
        'io.modelcontextprotocol/clientCapabilities': {'extensions': {'io.modelcontextprotocol/tasks': {}}},
    }
    headers = {
        'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': 'tools/call',
        'Mcp-Name': 'download_video',
    }
    params = {'name': 'download_video', 'arguments': {'url': VIDEO_URL}, '_meta': meta}

    async with running_app(), http_client() as client:
        response = await client.post(
            '/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params}
        )
        await jobs_done()

    task = response.json()['result']
    assert task['resultType'] == 'task'
    # ttlMs is required even when it is null, a dump without None values would drop it
    assert {'taskId', 'status', 'createdAt', 'lastUpdatedAt', 'ttlMs', 'pollIntervalMs'} <= set(task)
    assert (task['status'], task['ttlMs'], task['pollIntervalMs']) == ('working', None, 5000)
