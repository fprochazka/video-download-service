// Runs the script of index.html against a stubbed DOM and prints what it did as JSON, see test_index_page.py
// Usage: node index_page_harness.js <script.js> <scenario>
const fs = require('fs');

const [scriptPath, scenario] = process.argv.slice(2);
const source = fs.readFileSync(scriptPath, 'utf8');

const listeners = {};
const element = (id) => ({
    id, className: '', innerHTML: '', disabled: false, value: '', style: {},
    addEventListener(type, handler) { listeners[`${id}:${type}`] = handler; },
});
const elements = {};
const result = { fetches: [], assigned: [], intervals: 0 };
let fetchResponse = null;

const json = (status, body) => ({ ok: status < 300, status, json: async () => body });

global.document = { getElementById: (id) => (elements[id] ||= element(id)) };
global.window = {
    location: { pathname: '/', assign: (url) => result.assigned.push(url) },
    addEventListener(type, handler) { listeners[`window:${type}`] = handler; },
};
global.FormData = class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } };
global.fetch = async (url, options = {}) => {
    result.fetches.push({ url, method: options.method || 'GET', body: options.body ? options.body.entries : null });
    return fetchResponse;
};
global.setInterval = () => ++result.intervals;
global.clearInterval = () => {};

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
    const jobId = '019a3b2c-4d5e-7f00-8a1b-2c3d4e5f6a7b';
    if (scenario === 'submit') {
        eval(source);
        listeners['window:load']();
        elements.url.value = ' https://www.youtube.com/watch?v=dQw4w9WgXcQ ';
        fetchResponse = json(200, { download_id: jobId });
        await listeners['downloadForm:submit']({ preventDefault() {} });
    } else if (scenario === 'job-page') {
        window.location.pathname = `/jobs/${jobId}`;
        fetchResponse = json(200, { status: 'downloading', download_id: jobId, title: 'Some video' });
        eval(source);
        listeners['window:load']();
        await flush();
    } else if (scenario === 'job-completed') {
        window.location.pathname = `/jobs/${jobId}`;
        fetchResponse = json(200, {
            status: 'completed', download_id: jobId, title: 'Some <b>video</b>',
            files: ['Some_video-abc.mp4'],
            artifacts: [
                { name: 'Some_video-abc.mp4', kind: 'media', size: 52428800 },
                { name: 'Some_video-abc.en.vtt', kind: 'subtitle', size: 41000, lang: 'en', auto: false },
                { name: 'Some_video-abc.cs-orig.vtt', kind: 'subtitle', size: 39000, lang: 'cs-orig', auto: true },
                { name: 'info.json', kind: 'info', size: 350000 },
            ],
        });
        eval(source);
        listeners['window:load']();
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
    result.links = [...elements.status.innerHTML.matchAll(/<a href="([^"]+)" download>/g)].map((m) => m[1]);
    result.html = elements.status.innerHTML;
    result.submitDisabled = elements.submitBtn.disabled;
    console.log(JSON.stringify(result));
})();
