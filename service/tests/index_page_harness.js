// Runs the script of index.html against a stubbed DOM and prints what it did as JSON, see test_index_page.py
// Usage: node index_page_harness.js <script.js> <scenario>
const fs = require('fs');

const [scriptPath, scenario] = process.argv.slice(2);
const source = fs.readFileSync(scriptPath, 'utf8');

const listeners = {};
const element = (id) => ({
    id, className: '', innerHTML: '', disabled: false, value: '', style: {}, content: id === 'csrfToken' ? 'page-token' : '',
    addEventListener(type, handler) { listeners[`${id}:${type}`] = handler; },
});
const elements = {};
const result = { fetches: [], assigned: [], intervals: 0, intervalMs: [], clears: 0, confirms: [], alerts: [] };
let fetchResponse = null;
// When set, answers each request instead of fetchResponse
let fetchHandler = null;
let confirmAnswer = true;

const json = (status, body) => ({ ok: status < 300, status, json: async () => body, text: async () => String(body) });

global.document = { getElementById: (id) => (elements[id] ||= element(id)) };
global.window = {
    location: { pathname: '/', search: '', assign: (url) => result.assigned.push(url) },
    addEventListener(type, handler) { listeners[`window:${type}`] = handler; },
};
global.FormData = class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } };
global.fetch = async (url, options = {}) => {
    result.fetches.push({
        url, method: options.method || 'GET', body: options.body ? options.body.entries : null,
        csrf: (options.headers || {})['X-CSRF-Token'] || null,
    });
    return fetchHandler ? fetchHandler(url, options.method || 'GET') : fetchResponse;
};
global.confirm = (message) => { result.confirms.push(message); return confirmAnswer; };
global.alert = (message) => result.alerts.push(message);
global.setInterval = (handler, ms) => { result.intervalMs.push(ms); return ++result.intervals; };
global.clearInterval = () => { result.clears++; };

const completedJob = (jobId) => ({
    status: 'completed', download_id: jobId, title: 'Some <b>video</b>',
    files: ['Some_video-abc.mp4'],
    artifacts: [
        { name: 'Some_video-abc.mp4', kind: 'media', size: 52428800 },
        { name: 'Some_video-abc.en.vtt', kind: 'subtitle', size: 41000, lang: 'en', auto: false },
        { name: 'Some_video-abc.cs-orig.vtt', kind: 'subtitle', size: 39000, lang: 'cs-orig', auto: true },
        { name: 'info.json', kind: 'info', size: 350000 },
        { name: '"><img src=x onerror=alert(1)>.mp4', kind: 'media', size: 3 },
    ],
    deleted_files: [
        { name: 'Old-abc.en.vtt', kind: 'subtitle', size: 100, deleted_at: '2026-10-01T10:00:00+00:00', reason: 'gc' },
    ],
});

const transcriptArtifacts = [
    { name: 'Some_video-abc.transcript.md', kind: 'transcript', size: 2048, media: 'Some_video-abc.mp4' },
    { name: 'Some_video-abc.transcript.json', kind: 'transcript', size: 4096, media: 'Some_video-abc.mp4' },
];

const transcribableJob = (jobId, transcriptions) => {
    const job = completedJob(jobId);
    job.transcription_available = true;
    job.transcriptions = transcriptions;
    return job;
};

const downloadingJob = (jobId, progress) => ({
    status: 'downloading', download_id: jobId, title: 'Some <b>video</b>', uploader: 'jawed <i>', duration: 3725,
    resolution: '1920x1080', filesize_approx: 52428800,
    thumbnail: 'https://i.example.com/vi/abc/hq.jpg?a=1&b="><script>alert(1)</script>',
    progress,
});

const DOWNLOADING_STATES = {
    'job-downloading': {
        phase: 'downloading', part: 'video', part_index: 1, part_count: 2, downloaded_bytes: 12897484,
        total_bytes: 52428800, total_estimated: false, percent: 24.6, speed: 2202009.6, eta: 18,
        filename: 'Some_video-abc.f137.mp4',
    },
    'job-downloading-no-total': {
        phase: 'downloading', downloaded_bytes: 3145728, total_bytes: null, percent: null, speed: null, eta: null,
        fragment_index: 7, fragment_count: 120,
    },
    'job-downloading-estimate': {
        phase: 'downloading', downloaded_bytes: 1048576, total_bytes: 10485760, total_estimated: true, percent: 10,
        speed: 1024, eta: 3700,
    },
    'job-merging': { phase: 'merging', filename: 'Some_video-abc.mp4' },
    'job-extracted': null,
};

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const EVIL_MEDIA = '"><img src=x onerror=alert(1)>.mp4';

(async () => {
    const jobId = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b';
    if (scenario === 'submit' || scenario === 'submit-existing') {
        eval(source);
        listeners['window:load']();
        elements.url.value = ' https://www.youtube.com/watch?v=dQw4w9WgXcQ ';
        fetchResponse = json(200, { download_id: jobId, existing: scenario === 'submit-existing' });
        await listeners['downloadForm:submit']({ preventDefault() {} });
    } else if (scenario === 'existing-completed' || scenario === 'existing-downloading' || scenario === 'existing-absent') {
        window.location.pathname = `/jobs/${jobId}`;
        window.location.search = scenario === 'existing-absent' ? '' : '?existing=1';
        const job = scenario === 'existing-downloading' ? downloadingJob(jobId, null) : completedJob(jobId);
        Object.assign(job, {
            url: 'https://youtu.be/dQw4w9WgXcQ?si=<b>x</b>', created_at: '2026-10-01T10:00:00+00:00',
            completed_at: '2026-10-01T10:05:00+00:00',
        });
        fetchHandler = (url, method) => (method === 'POST' ? json(200, { download_id: 'new-job', existing: false }) : json(200, job));
        eval(source);
        listeners['window:load']();
        await flush();
        if (scenario === 'existing-completed') {
            listeners['notice:click']({ target: { dataset: { downloadAgain: '1' } } });
            await flush();
            await flush();
        }
    } else if (scenario === 'job-page') {
        window.location.pathname = `/jobs/${jobId}`;
        fetchResponse = json(200, { status: 'downloading', download_id: jobId, title: 'Some video' });
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario in DOWNLOADING_STATES || scenario === 'job-unsafe-thumbnail') {
        window.location.pathname = `/jobs/${jobId}`;
        const job = downloadingJob(jobId, DOWNLOADING_STATES[scenario] || null);
        if (scenario === 'job-unsafe-thumbnail') job.thumbnail = 'javascript:alert(1)';
        fetchResponse = json(200, job);
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'job-completed') {
        window.location.pathname = `/jobs/${jobId}`;
        fetchResponse = json(200, completedJob(jobId));
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'delete-file' || scenario === 'delete-cancelled' || scenario === 'delete-refused') {
        window.location.pathname = `/jobs/${jobId}`;
        const afterDelete = completedJob(jobId);
        afterDelete.artifacts = afterDelete.artifacts.filter((a) => a.kind !== 'media');
        afterDelete.deleted_files = [
            { name: 'Some_video-abc.mp4', kind: 'media', size: 52428800, deleted_at: '2026-10-31T10:00:00+00:00', reason: 'user' },
            ...afterDelete.deleted_files,
        ];
        let deleted = false;
        fetchHandler = (url, method) => {
            if (method === 'DELETE') {
                if (scenario === 'delete-refused') return json(409, { detail: 'The download is still running.' });
                deleted = true;
                return json(200, afterDelete);
            }
            return json(200, deleted ? afterDelete : completedJob(jobId));
        };
        confirmAnswer = scenario !== 'delete-cancelled';
        eval(source);
        listeners['window:load']();
        await flush();
        listeners['status:click']({ target: { dataset: { deleteFile: 'Some_video-abc.mp4' } } });
        await flush();
        await flush();
    } else if (scenario === 'transcription-running' || scenario === 'transcription-failed' || scenario === 'transcription-completed') {
        window.location.pathname = `/jobs/${jobId}`;
        const states = {
            'transcription-running': { status: 'running', model: 'gemini-3.5-flash-lite', files: [] },
            'transcription-failed': {
                status: 'failed', error: 'Gemini API error 429 <RESOURCE_EXHAUSTED>', error_code: 'rate_limited', files: [],
                usage: { prompt_tokens: 1000, prompt_audio_tokens: 750, output_tokens: 200, thinking_tokens: 0 },
                cost: { usd: 0.0008, estimated: true, prices: { model: 'gemini-3.5-flash-lite', input_text: 0.3, input_audio: 0.3, output: 2.5, as_of: '2026-10-30' } },
            },
            'transcription-completed': {
                status: 'completed', model: 'gemini-3.5-flash-lite', files: transcriptArtifacts.map((a) => a.name),
                warnings: [{ code: 'ends_early', message: 'The transcript ends at <00:04:00>.' }],
            },
        };
        const job = transcribableJob(jobId, { 'Some_video-abc.mp4': states[scenario], [EVIL_MEDIA]: { status: 'pending', files: [] } });
        if (scenario !== 'transcription-running') delete job.transcriptions[EVIL_MEDIA];
        if (scenario === 'transcription-completed') job.artifacts.push(...transcriptArtifacts);
        fetchResponse = json(200, job);
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'transcription-costs') {
        window.location.pathname = `/jobs/${jobId}`;
        const usage = { prompt_tokens: 11293, prompt_audio_tokens: 8924, output_tokens: 4019, thinking_tokens: 0 };
        const job = transcribableJob(jobId, {
            'Some_video-abc.mp4': {
                status: 'completed', model: 'gemini-3.5-flash-lite', files: [], usage,
                cost: {
                    usd: 0.013435, estimated: true,
                    prices: { model: 'gemini-3.5-flash-lite', input_text: 0.3, input_audio: 0.3, output: 2.5, as_of: '2026-10-30' },
                },
            },
            [EVIL_MEDIA]: {
                status: 'completed', model: 'my-<b>model</b>', files: [], cost: null,
                usage: { prompt_tokens: 100, output_tokens: 20, thinking_tokens: 3 },
            },
        });
        fetchResponse = json(200, job);
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'transcription-costs-per-hour') {
        window.location.pathname = `/jobs/${jobId}`;
        // What /status returns for a provider that bills the length of the audio: the token counts are their defaults
        const usage = { prompt_tokens: 0, output_tokens: 0, thinking_tokens: 0, prompt_audio_tokens: null, audio_seconds: 356.2 };
        const job = transcribableJob(jobId, {
            'Some_video-abc.mp4': {
                status: 'completed', provider: 'elevenlabs', model: 'scribe_v2', files: [], usage,
                cost: {
                    usd: 0.021817, estimated: true,
                    prices: { model: 'scribe_v2', per_hour: 0.22, as_of: '2026-11-01', input_text: null, input_audio: null, output: null },
                },
            },
            [EVIL_MEDIA]: { status: 'completed', model: 'scribe_<b>v9</b>', files: [], cost: null, usage },
        });
        fetchResponse = json(200, job);
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'transcription-costs-reported') {
        window.location.pathname = `/jobs/${jobId}`;
        const usage = { prompt_tokens: 0, output_tokens: 0, thinking_tokens: 0, prompt_audio_tokens: null, audio_seconds: 5.0135 };
        const prices = { model: 'scribe_v2', per_hour: 0.22, as_of: '2026-11-01', input_text: null, input_audio: null, output: null };
        const job = transcribableJob(jobId, {
            'Some_video-abc.mp4': {
                status: 'completed', provider: 'elevenlabs', model: 'scribe_v2', files: [], usage,
                cost: { usd: 0.000364, estimated: false, source: 'provider', estimate_usd: 0.000367, prices },
                provider_report: { transcription_id: 'tr-1', credits: 1, cost: { amount: 0.000364, currency: 'usd' } },
            },
            // Reported, with no list price and no report: the amount alone
            [EVIL_MEDIA]: {
                status: 'completed', provider: 'elevenlabs', model: 'scribe_<b>v9</b>', files: [], usage,
                cost: { usd: 1.5, estimated: false, source: 'provider', estimate_usd: null, prices: null },
                provider_report: null,
            },
        });
        fetchResponse = json(200, job);
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'transcribe-click' || scenario === 'transcribe-refused') {
        window.location.pathname = `/jobs/${jobId}`;
        let requested = false;
        fetchHandler = (url, method) => {
            if (method === 'POST') {
                if (scenario === 'transcribe-refused') return json(409, { detail: 'This file is already being transcribed.' });
                requested = true;
            }
            return json(requested && method === 'POST' ? 202 : 200, transcribableJob(jobId, requested ? { [EVIL_MEDIA]: { status: 'pending', files: [] } } : {}));
        };
        eval(source);
        listeners['window:load']();
        await flush();
        listeners['status:click']({ target: { dataset: { transcribeFile: EVIL_MEDIA } } });
        await flush();
        await flush();
    } else if (scenario === 'transcript-show') {
        window.location.pathname = `/jobs/${jobId}`;
        const job = transcribableJob(jobId, {});
        job.artifacts.push(...transcriptArtifacts);
        fetchHandler = (url) => (url.endsWith('.transcript.md')
            ? json(200, '# Some video\n\n**[00:01] Speaker 1:** <script>alert(1)</script>')
            : json(200, job));
        eval(source);
        listeners['window:load']();
        await flush();
        listeners['status:click']({ target: { dataset: { showTranscript: 'Some_video-abc.transcript.md' } } });
        await flush();
        await flush();
    } else if (scenario === 'job-not-found') {
        window.location.pathname = `/jobs/${jobId}`;
        fetchResponse = json(404, { detail: 'Download not found' });
        eval(source);
        listeners['window:load']();
        await flush();
    } else {
        throw new Error(`Unknown scenario ${scenario}`);
    }
    result.status = { className: elements.status.className, text: elements.status.innerHTML.replace(/<[^>]+>/g, '') };
    const html = elements.status.innerHTML;
    result.links = [...html.matchAll(/<a href="([^"]+)" download>/g)].map((m) => m[1]);
    result.groups = [...html.matchAll(/<h4 class="files-group">([^<]+)<\/h4>/g)].map((m) => m[1]);
    result.rows = [...html.matchAll(/<li class="file-row">(.*?)<\/li>/g)].map((m) => m[1].replace(/<a [^>]+>([^<]*)<\/a>/g, '[$1]').replace(/<button [^>]*data-delete-file="([^"]*)"[^>]*>([^<]*)<\/button>/g, '[$2 $1]').replace(/<button [^>]*>([^<]*)<\/button>/g, '[$1]').replace(/<[^>]+>/g, '|'));
    result.buttons = [...html.matchAll(/<button ([^>]*)>([^<]*)<\/button>/g)].map((m) => ({
        text: m[2], disabled: / disabled( |$)/.test(` ${m[1]}`), title: (m[1].match(/title="([^"]*)"/) || [])[1] || null,
        transcribe: (m[1].match(/data-transcribe-file="([^"]*)"/) || [])[1] || null,
    }));
    result.transcriptions = [...html.matchAll(/<div class="file-transcription( failed)?">(.*?)<\/div>/g)].map((m) => m[2].replace(/<[^>]+>/g, '|'));
    result.costs = [...html.matchAll(/<span class="file-cost" title="([^"]*)">([^<]*)<\/span>/g)].map((m) => ({ title: m[1], text: m[2] }));
    result.transcriptText = [...html.matchAll(/<pre class="transcript-text">(.*?)<\/pre>/gs)].map((m) => m[1]);
    result.thumbnails = [...html.matchAll(/<img ([^>]*)>/g)].map((m) => m[1]);
    result.progressWidths = [...html.matchAll(/<div class="progress-fill" style="width: ([^"]*)">/g)].map((m) => m[1]);
    result.progressText = [...html.matchAll(/<small class="progress-text">([^<]*)<\/small>/g)].map((m) => m[1]);
    result.deleted = [...html.matchAll(/<li class="file-deleted">(.*?)<\/li>/g)].map((m) => m[1]);
    result.html = elements.status.innerHTML;
    result.notice = { className: elements.notice.className, html: elements.notice.innerHTML };
    result.submitDisabled = elements.submitBtn.disabled;
    console.log(JSON.stringify(result));
})();
