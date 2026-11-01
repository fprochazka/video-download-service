import json
from contextlib import asynccontextmanager

import httpx2
import jsonschema
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.utilities.asgi_transport import StreamingASGITransport, run_asgi_lifespan

from tests.conftest import BASE_URL
from tests.ytdlp_fakes import FakeYoutubeDL, raise_like_ytdlp, read_metadata, write_media
from video_download import config, mcp_server, web

VIDEO_URL = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'
MEDIA = 'Me_at_the_zoo-jNQXAC9IVRw.mp4'


def video(opts, url):
    path = write_media(opts, MEDIA)
    return {
        'id': 'jNQXAC9IVRw',
        'title': 'Me at the zoo',
        'duration': 19,
        'uploader': 'jawed',
        'requested_downloads': [{'filepath': path}],
    }


@asynccontextmanager
async def running_app():
    async with run_asgi_lifespan(web.app):
        yield


def http_client(host='localhost', headers=None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=StreamingASGITransport(web.app), base_url=f'http://{host}', headers=headers)


class PlainClient(Client):
    """A client without the tasks extension, which fastmcp_tasks adds to every Client once a test imports it"""

    _auto_internal_extensions = False


@asynccontextmanager
async def mcp_client(host='localhost', headers=None, client_class=PlainClient):
    """A client of the app's /mcp endpoint over HTTP, in process"""

    def factory(**kwargs):
        return httpx2.AsyncClient(transport=StreamingASGITransport(web.app), **kwargs)

    transport = StreamableHttpTransport(url=f'http://{host}/mcp', headers=headers, httpx_client_factory=factory)
    async with client_class(transport) as client:
        yield client


async def jobs_done():
    await web.app.state.worker.join()


@pytest.mark.anyio
async def test_lists_the_tools_with_their_annotations(downloads_dir, genai):
    async with running_app(), mcp_client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        templates = {template.uri_template for template in await client.list_resource_templates()}

    assert set(tools) == {'download_video', 'get_job', 'transcribe_file'}
    assert tools['get_job'].annotations.read_only_hint is True
    assert tools['get_job'].annotations.idempotent_hint is True
    assert tools['download_video'].annotations.read_only_hint is False
    assert tools['transcribe_file'].annotations.read_only_hint is False
    assert set(tools['download_video'].input_schema['properties']) == {'url', 'force'}
    assert {tool.title for tool in tools.values()} == {
        'Download a video',
        'Get a download job',
        'Transcribe a downloaded file',
    }
    assert all(tool.output_schema['type'] == 'object' for tool in tools.values())
    assert templates == {
        'video-download://jobs/{download_id}/metadata',
        'video-download://jobs/{download_id}/log',
        'video-download://jobs/{download_id}/transcripts/{filename}',
    }


@pytest.mark.anyio
async def test_the_tool_descriptions_tell_an_agent_how_to_use_the_tools(downloads_dir, genai):
    async with running_app(), mcp_client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        instructions = client.instructions

    download = tools['download_video']
    assert 'any site yt-dlp supports' in download.description
    assert 'playlists, channels, profiles or other collections' in download.description
    assert 'follow it with get_job' in download.description
    assert 'plain HTTP download URL' in download.description
    assert 'video-download://jobs/{id}/' in download.description
    assert 'youtube' not in download.description.lower() + str(download.input_schema).lower()
    assert 'ONE video' in download.input_schema['properties']['url']['description']
    assert 'when the user asks for a transcript' in download.description
    assert 'transcribe a video file with transcribe_file' in download.description
    assert 'longer with transcribe' not in download.description
    assert 'with Gemini, about $0.13 per hour of speech.' in tools['transcribe_file'].description
    assert 'when the user asks for a transcript' in tools['transcribe_file'].description
    assert 'no subtitle files' in tools['transcribe_file'].description
    get_job = tools['get_job']
    assert 'pending, downloading, transcribing' in get_job.description
    wait = get_job.input_schema['properties']['wait_seconds']
    assert (wait['minimum'], wait['maximum'], wait['default']) == (0, 60, 0)
    download_id = get_job.input_schema['properties']['download_id']
    assert (download_id['format'], download_id['type']) == ('uuid', 'string')
    assert 'returned' in download_id['description']
    transcribe = tools['transcribe_file']
    assert 'replaces an earlier one' in transcribe.description
    assert 'video or audio files' in transcribe.input_schema['properties']['filename']['description']
    assert 'title' not in get_job.output_schema['properties']['state']
    assert "Prefer the job's subtitle files" in instructions
    assert 'when the user asks for one' in instructions
    assert 'transcribe=true' not in instructions
    assert 'youtube' not in instructions.lower()


@pytest.mark.anyio
async def test_without_gemini_the_server_offers_no_transcription(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        instructions = client.instructions
        with pytest.raises(ToolError, match='Unexpected keyword argument'):
            await client.call_tool('download_video', {'url': VIDEO_URL, 'transcribe': True})
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        await jobs_done()
        job = await client.call_tool('get_job', {'download_id': started.structured_content['download_id']})

    assert set(tools) == {'download_video', 'get_job'}
    download = tools['download_video']
    assert set(download.input_schema['properties']) == {'url', 'force'}
    assert download.input_schema['additionalProperties'] is False
    texts = [instructions, download.description, tools['get_job'].description]
    assert not [text for text in texts if 'transcri' in text.lower() or '$' in text]
    jsonschema.validate(started.structured_content, download.output_schema)
    jsonschema.validate(job.structured_content, tools['get_job'].output_schema)
    assert job.structured_content['state'] == 'completed'
    assert len(FakeYoutubeDL.instances) == 1


@pytest.mark.anyio
async def test_structured_content_matches_the_advertised_output_schema(
    downloads_dir, public_base_url, genai, fake_audio
):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        schemas = {tool.name: tool.output_schema for tool in await client.list_tools()}
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        transcribing = await client.call_tool('transcribe_file', {'download_id': download_id, 'filename': MEDIA})
        await jobs_done()
        transcribed = await client.call_tool('get_job', {'download_id': download_id})
        FakeYoutubeDL.behaviour = lambda opts, url: raise_like_ytdlp(Exception('Video unavailable'))
        failing = await client.call_tool('download_video', {'url': VIDEO_URL, 'force': True})
        await jobs_done()
        failed = await client.call_tool('get_job', {'download_id': failing.structured_content['download_id']})

    state = transcribed.structured_content['transcriptions'][MEDIA]
    assert (state['cost_usd'] > 0, state['cost_estimated']) == (True, True)
    assert state['provider_report']['usage_metadata']['prompt_token_count'] == 1000
    assert failed.structured_content['error_code'] == 'unavailable'
    for name, result in [
        ('download_video', started),
        ('get_job', transcribed),
        ('transcribe_file', transcribing),
        ('download_video', failing),
        ('get_job', failed),
    ]:
        jsonschema.validate(result.structured_content, schemas[name])


@pytest.mark.anyio
async def test_download_video_answers_with_the_job_and_get_job_follows_it(downloads_dir, public_base_url):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL + '&list=RDjNQXAC9IVRw'})
        download_id = started.structured_content['download_id']
        assert started.structured_content['state'] in ('pending', 'downloading')
        assert started.structured_content['done'] is False
        assert f'Call get_job with download_id="{download_id}"' in started.content[0].text

        await jobs_done()
        result = await client.call_tool('get_job', {'download_id': download_id})

    job = result.structured_content
    assert job['state'] == 'completed'
    assert job['done'] is True
    assert job['title'] == 'Me at the zoo'
    assert job['duration'] == 19
    assert job['uploader'] == 'jawed'
    assert job['job_url'] == f'{BASE_URL}/jobs/{download_id}'
    media, info = job['files']
    assert media == {
        'name': MEDIA,
        'kind': 'media',
        'size': 5,
        'url': f'{BASE_URL}/downloads/{download_id}/{MEDIA}',
        'media': None,
        'lang': None,
        'auto_captions': None,
        'resource_uri': None,
    }
    assert (info['name'], info['kind'], info['url']) == (
        'info.json',
        'info',
        f'{BASE_URL}/downloads/{download_id}/info.json',
    )
    assert job['resources'] == {
        'metadata': f'video-download://jobs/{download_id}/metadata',
        'log': f'video-download://jobs/{download_id}/log',
    }
    assert job['transcriptions'] == {}
    text = result.content[0].text
    assert f'Job {download_id}: completed. "Me at the zoo" 19 s by jawed' in text
    assert f'- media {MEDIA} (5 B): {BASE_URL}/downloads/{download_id}/{MEDIA}' in text
    assert 'still running' not in text
    links = result.content[1:]
    assert {link.type for link in links} == {'resource_link'}
    assert [str(link.uri) for link in links] == [media['url'], info['url']]
    assert read_metadata(downloads_dir, download_id)['download_url'] == VIDEO_URL


@pytest.mark.anyio
async def test_file_urls_fall_back_to_the_forwarded_host_of_the_request(downloads_dir, monkeypatch):
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    FakeYoutubeDL.behaviour = video
    forwarded = {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'localhost'}

    async with running_app(), mcp_client(headers=forwarded) as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})

    assert (
        started.structured_content['job_url'] == f'https://localhost/jobs/{started.structured_content["download_id"]}'
    )


@pytest.mark.anyio
async def test_download_video_rejects_a_playlist_without_creating_a_job(downloads_dir):
    async with running_app(), mcp_client() as client:
        with pytest.raises(ToolError, match='Playlists, channels, profiles and other collections are not supported'):
            await client.call_tool('download_video', {'url': 'https://www.youtube.com/playlist?list=PLx0sYbCqOb8T'})

    assert list(downloads_dir.iterdir()) == []
    assert FakeYoutubeDL.instances == []


@pytest.mark.anyio
async def test_download_video_never_transcribes_and_rejects_a_transcribe_argument(downloads_dir, genai):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        with pytest.raises(ToolError, match='Unexpected keyword argument'):
            await client.call_tool('download_video', {'url': VIDEO_URL, 'transcribe': True})
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        await jobs_done()
        result = await client.call_tool('get_job', {'download_id': started.structured_content['download_id']})

    assert result.structured_content['state'] == 'completed'
    assert result.structured_content['transcriptions'] == {}
    assert genai.requests == []
    assert len(FakeYoutubeDL.instances) == 1


@pytest.mark.anyio
async def test_a_transcript_of_transcribe_file_is_listed_and_readable_as_a_resource(
    downloads_dir, public_base_url, genai, fake_audio
):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        await client.call_tool('transcribe_file', {'download_id': download_id, 'filename': MEDIA})
        await jobs_done()
        result = await client.call_tool('get_job', {'download_id': download_id})
        transcript = await client.read_resource(
            f'video-download://jobs/{download_id}/transcripts/Me_at_the_zoo-jNQXAC9IVRw.transcript.md'
        )

    job = result.structured_content
    assert job['state'] == 'completed'
    state = job['transcriptions'][MEDIA]
    assert state['status'] == 'completed'
    assert state['files'] == ['Me_at_the_zoo-jNQXAC9IVRw.transcript.md', 'Me_at_the_zoo-jNQXAC9IVRw.transcript.json']
    assert state['cost_usd'] > 0
    uri = f'video-download://jobs/{download_id}/transcripts/Me_at_the_zoo-jNQXAC9IVRw.transcript.md'
    assert state['transcript_resource_uri'] == uri
    assert [f['kind'] for f in job['files']] == ['media', 'info', 'transcript', 'transcript']
    assert f'Transcription of {MEDIA}: completed, estimated cost $' in result.content[0].text
    assert uri in {str(block.uri) for block in result.content if block.type == 'resource_link'}
    (content,) = transcript
    assert content.mime_type == 'text/markdown'
    assert content.text.startswith('# Me at the zoo\n')
    assert '**[00:01] Speaker 1:** Hello and welcome. Today we talk about engines.' in content.text
    assert len(genai.requests) == 1


@pytest.mark.anyio
async def test_a_job_with_a_queued_transcription_is_still_running(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        metadata = read_metadata(downloads_dir, download_id)
        metadata['transcriptions'] = {MEDIA: {'status': 'pending'}, 'other.mp4': {'status': 'completed'}}
        (downloads_dir / download_id / 'metadata.json').write_text(json.dumps(metadata))
        result = await client.call_tool('get_job', {'download_id': download_id})

    assert (result.structured_content['state'], result.structured_content['message']) == (
        'transcribing',
        'transcribing 2/2',
    )
    assert result.structured_content['done'] is False


@pytest.mark.anyio
async def test_the_transcription_price_follows_the_provider(downloads_dir, scribe):
    async with running_app(), mcp_client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    description = tools['transcribe_file'].description
    assert description.startswith(
        'Transcribe one video or audio file of a completed job with ElevenLabs Scribe, $0.22 per'
    )
    assert 'Gemini' not in description


@pytest.mark.anyio
async def test_a_model_without_a_measured_price_only_says_it_costs_money(downloads_dir, genai, monkeypatch):
    monkeypatch.setenv('TRANSCRIPTION_MODEL', 'gemini-3.8-flash')

    async with running_app(), mcp_client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert 'with Gemini, which costs money.' in tools['transcribe_file'].description


@pytest.mark.anyio
async def test_transcribe_file_transcribes_a_downloaded_media_file(downloads_dir, genai, fake_audio):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        queued = await client.call_tool('transcribe_file', {'download_id': download_id, 'filename': MEDIA})
        assert queued.structured_content['state'] == 'transcribing'
        await jobs_done()
        result = await client.call_tool('get_job', {'download_id': download_id})
        with pytest.raises(ToolError, match='File not found'):
            await client.call_tool('transcribe_file', {'download_id': download_id, 'filename': 'metadata.json'})

    assert result.structured_content['transcriptions'][MEDIA]['status'] == 'completed'


@pytest.mark.anyio
async def test_transcribe_file_is_a_tool_error_when_the_key_is_gone(downloads_dir, genai, monkeypatch):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        monkeypatch.delenv('GEMINI_API_KEY')
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        await jobs_done()
        with pytest.raises(ToolError, match='Transcription is not configured'):
            await client.call_tool(
                'transcribe_file', {'download_id': started.structured_content['download_id'], 'filename': MEDIA}
            )


@pytest.mark.anyio
async def test_get_job_wait_seconds_returns_once_the_job_changes(downloads_dir, monkeypatch):
    monkeypatch.setattr(mcp_server, 'WAIT_POLL_SECONDS', 0.01)
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        # The download runs while get_job waits, the first metadata write ends the wait long before 60 s
        result = await client.call_tool(
            'get_job', {'download_id': started.structured_content['download_id'], 'wait_seconds': 60}, timeout=10
        )
        await jobs_done()

    assert result.structured_content['updated_at'] != started.structured_content['updated_at']


@pytest.mark.anyio
async def test_get_job_wait_seconds_waits_at_most_the_cap(downloads_dir, monkeypatch):
    monkeypatch.setattr(mcp_server, 'MAX_WAIT_SECONDS', 0.2)
    monkeypatch.setattr(mcp_server, 'WAIT_POLL_SECONDS', 0.01)
    download_id = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'
    (downloads_dir / download_id).mkdir(parents=True)
    metadata = {'download_id': download_id, 'url': VIDEO_URL, 'status': 'downloading', 'files': []}
    metadata |= {'created_at': '2026-11-01T10:00:00+00:00', 'updated_at': '2026-11-01T10:00:00+00:00'}
    # Written after the startup sweep, which would fail a downloading job
    async with running_app():
        (downloads_dir / download_id / 'metadata.json').write_text(json.dumps(metadata))
        async with mcp_client() as client:
            result = await client.call_tool('get_job', {'download_id': download_id, 'wait_seconds': 60}, timeout=5)

    assert result.structured_content['state'] == 'downloading'


@pytest.mark.anyio
async def test_unknown_jobs_are_tool_and_resource_errors(downloads_dir, genai):
    async with running_app(), mcp_client() as client:
        with pytest.raises(ToolError, match='there is no such job'):
            await client.call_tool('get_job', {'download_id': '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b'})
        with pytest.raises(ToolError, match='it is not a job id'):
            await client.call_tool('get_job', {'download_id': '../etc'})
        with pytest.raises(ToolError, match='there is no such job'):
            await client.call_tool(
                'transcribe_file', {'download_id': '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b', 'filename': MEDIA}
            )
        with pytest.raises(Exception, match='no metadata.json'):
            await client.read_resource('video-download://jobs/019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b/metadata')


@pytest.mark.anyio
async def test_resources_serve_the_metadata_and_the_log_but_only_transcripts(downloads_dir):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        (metadata,) = await client.read_resource(f'video-download://jobs/{download_id}/metadata')
        (log,) = await client.read_resource(f'video-download://jobs/{download_id}/log')
        with pytest.raises(Exception, match='is not a transcript'):
            await client.read_resource(f'video-download://jobs/{download_id}/transcripts/{MEDIA}')
        with pytest.raises(Exception, match='has no x.transcript.md'):
            await client.read_resource(f'video-download://jobs/{download_id}/transcripts/x.transcript.md')

    assert metadata.mime_type == 'application/json'
    assert json.loads(metadata.text)['status'] == 'completed'
    assert log.mime_type == 'text/plain'
    assert (downloads_dir / download_id / 'download.log').exists()


@pytest.mark.anyio
async def test_the_endpoint_refuses_a_host_it_is_not_served_at(downloads_dir):
    # A DNS rebinding page reaches the server's address with its own name in the Host header
    async with running_app(), http_client(headers={'Host': 'attacker.example'}) as client:
        response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})

    assert response.status_code == 421


def test_allowed_hosts_are_the_public_host_and_the_configured_ones(monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://video-download.example.ts.net')
    monkeypatch.setenv('MCP_ALLOWED_HOSTS', 'video-download, 100.64.0.1 ')

    assert config.mcp_allowed_hosts() == ['video-download.example.ts.net', 'video-download', '100.64.0.1']


@pytest.mark.anyio
async def test_get_job_has_the_cost_elevenlabs_reported(downloads_dir, public_base_url, scribe, fake_audio):
    FakeYoutubeDL.behaviour = video

    async with running_app(), mcp_client() as client:
        schemas = {tool.name: tool.output_schema for tool in await client.list_tools()}
        started = await client.call_tool('download_video', {'url': VIDEO_URL})
        download_id = started.structured_content['download_id']
        await jobs_done()
        await client.call_tool('transcribe_file', {'download_id': download_id, 'filename': MEDIA})
        await jobs_done()
        result = await client.call_tool('get_job', {'download_id': download_id})

    jsonschema.validate(result.structured_content, schemas['get_job'])
    state = result.structured_content['transcriptions'][MEDIA]
    assert (state['cost_usd'], state['cost_estimated']) == (0.000364, False)
    assert state['provider_report']['transcription_id'] == 'tr-123'
    assert state['provider_report']['credits'] == 1
    assert state['provider_report']['cost'] == {'amount': 0.000364, 'currency': 'usd'}
    assert f'Transcription of {MEDIA}: completed, cost $0.000364 as reported by the provider' in result.content[0].text
