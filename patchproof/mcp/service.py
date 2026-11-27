"""Single-owner persistent job queue with killable per-image workers."""
import asyncio
import hashlib
import io
import json
import multiprocessing as mp
from pathlib import Path
import sqlite3
import time
import uuid
from PIL import Image
from .worker import analyze


class Service:
    def __init__(self, directory, roots=(), timeout=240):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.roots = [Path(p).resolve() for p in roots]
        self.timeout = timeout
        self.db = sqlite3.connect(self.directory / 'jobs.sqlite', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, key TEXT UNIQUE, digest TEXT, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, body TEXT)')
        for row in self.db.execute('SELECT body FROM jobs').fetchall():
            job = json.loads(row[0])
            if job['status'] in ('queued','running','cancelling'):
                job['status'] = 'interrupted'
                self.save(job)
        self.queue = asyncio.Queue()

    def save(self, job):
        self.db.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job), job['jobId']))
        self.db.commit()

    def get(self, job_id):
        row = self.db.execute('SELECT body FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown jobId')
        return json.loads(row[0])

    def ingest(self, data):
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('PNG exceeds 20 MiB')
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.width * image.height > 4_000_000:
                raise ValueError('Expected PNG with at most 4 million pixels')
            image.load()
            rgba = image.convert('RGBA')
        asset_id = uuid.uuid4().hex
        rgba.save(self.directory / (asset_id + '.png'))
        info = {'assetId':asset_id, 'width':rgba.width,'height':rgba.height,
                'pixelFingerprint':'rgba8-sha256:' + hashlib.sha256(rgba.width.to_bytes(8,'big') + rgba.height.to_bytes(8,'big') + rgba.tobytes()).hexdigest()}
        self.db.execute('INSERT INTO assets VALUES (?,?)', (asset_id,json.dumps(info)))
        self.db.commit()
        return info

    def submit(self, request):
        encoded = json.dumps(request, sort_keys=True, separators=(',',':'))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = self.db.execute('SELECT digest,body FROM jobs WHERE key=?', (request['requestKey'],)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError('requestKey conflict: input differs')
            return self.public(json.loads(existing[1]))
        items = []
        for item in request['items']:
            item = dict(item)
            try:
                if item.get('localPath'):
                    path = Path(item['localPath']).resolve(strict=True)
                    if not any(path.is_relative_to(root) for root in self.roots):
                        raise ValueError('Path outside authorized roots')
                    with path.open('rb') as stream:
                        info = self.ingest(stream.read(20 * 1024 * 1024 + 1))
                    item['assetId'] = info['assetId']
                if not self.db.execute('SELECT 1 FROM assets WHERE id=?', (item.get('assetId'),)).fetchone():
                    raise ValueError('Unknown assetId')
            except Exception as exc:
                item['error'] = {'code':'INVALID_INPUT','message':str(exc)}
            items.append(item)
        job = {'jobId':uuid.uuid4().hex,'status':'queued','total':len(items),'completed':0,'items':items,'results':[], 'options':request['options']}
        self.db.execute('INSERT INTO jobs VALUES (?,?,?,?)', (job['jobId'],request['requestKey'],digest,json.dumps(job)))
        self.db.commit()
        self.queue.put_nowait(job['jobId'])
        return self.public(job)

    @staticmethod
    def public(job):
        return {k:v for k,v in job.items() if k not in ('items','options')}

    async def run(self):
        while True:
            job_id = await self.queue.get()
            job = self.get(job_id)
            if job['status'] == 'cancelled':
                continue
            job['status'] = 'running'
            self.save(job)
            for item in job['items']:
                if self.get(job_id)['status'] == 'cancelling':
                    job['status'] = 'cancelled'
                    break
                result = {'clientItemId':item['clientItemId'],'assetId':item.get('assetId')}
                if 'error' in item:
                    result['error'] = item['error']
                else:
                    folder = self.directory / job_id / str(job['completed'])
                    folder.mkdir(parents=True, exist_ok=True)
                    ctx = mp.get_context('spawn')
                    receiver, sender = ctx.Pipe(duplex=False)
                    process = ctx.Process(target=analyze,args=(str(self.directory / (item['assetId']+'.png')),str(folder),job['options']['threshold'],job['options']['previewSizes'],sender))
                    process.start()
                    sender.close()
                    started = time.monotonic()
                    try:
                        while not receiver.poll():
                            if self.get(job_id)['status'] == 'cancelling':
                                job['status'] = 'cancelled'
                                break
                            if not process.is_alive() or time.monotonic()-started > self.timeout:
                                raise TimeoutError('Detection worker exited or timed out')
                            await asyncio.sleep(.05)
                        if job['status'] != 'cancelled':
                            result.update(receiver.recv())
                            result['artifacts'] = {k: ([f'{job_id}/{job["completed"]}/{v}.png' for v in value] if isinstance(value,list) else f'{job_id}/{job["completed"]}/{value}.png') for k,value in result.get('artifacts',{}).items()}
                    except Exception as exc:
                        result['error'] = {'code':'WORKER_FAILED','message':str(exc)}
                    finally:
                        if process.is_alive():
                            process.terminate()
                        process.join()
                        receiver.close()
                    if job['status'] == 'cancelled':
                        break
                job['results'].append(result)
                job['completed'] += 1
                self.save(job)
            if job['status'] != 'cancelled':
                job['status'] = 'completed_with_errors' if any('error' in r for r in job['results']) else 'completed'
            self.save(job)
