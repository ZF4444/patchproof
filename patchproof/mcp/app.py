"""Local authenticated Streamable HTTP host (MCP SDK 1.x)."""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from importlib.metadata import version as package_version
from pathlib import Path
import secrets
from typing import Annotated
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Mount, Route
from patchproof import __version__
from .contracts import Capabilities, JobResult, JobPage, PreviewResult
from .service import Service


class Item(BaseModel):
    clientItemId: str = Field(min_length=1,max_length=256,description="Caller resource identity, unique within the batch and echoed in results.")
    assetId: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$', description='Asset ID returned by POST /v1/assets; mutually exclusive with localPath.')
    localPath: str | None = Field(default=None, description="Local PNG under an authorized root; snapshotted on submission. Mutually exclusive with assetId.")

    @model_validator(mode='after')
    def exclusive(self):
        if (self.assetId is None) == (self.localPath is None):
            raise ValueError('Exactly one of assetId and localPath is required')
        return self


class Size(BaseModel):
    width: int = Field(ge=1,le=2048,description="Preview dimension in pixels; must exceed the detected fixed borders.")
    height: int = Field(ge=1,le=2048,description="Preview dimension in pixels; must exceed the detected fixed borders.")


class Options(BaseModel):
    threshold: float = Field(default=6,ge=0,le=255,allow_inf_nan=False,description="Maximum detection distortion metric, not confidence; lower is stricter.")
    previewSizes: list[Size] = Field(default_factory=list,max_length=4,description="Optional stretch preview sizes; at most four.")


def create_app(directory, roots=(), token=None, timeout=240, *, allowed_hosts=None, allowed_origins=None):
    service = Service(directory, roots, timeout)
    if not token:
        raise ValueError('An access token is required')
    allowed_hosts = set(allowed_hosts if allowed_hosts is not None else ('127.0.0.1:18741', 'localhost:18741'))
    allowed_origins = set(allowed_origins if allowed_origins is not None else ('http://127.0.0.1:18741', 'http://localhost:18741'))
    mcp = FastMCP('PatchProof',stateless_http=True,json_response=True,
                  transport_security=TransportSecuritySettings(
                      allowed_hosts=list(allowed_hosts), allowed_origins=list(allowed_origins)))

    # SDK 1.26.0 exposes no FastMCP version constructor argument. Set the
    # underlying server version before initialize options are created.
    mcp._mcp_server.version = __version__

    @mcp.tool(structured_output=True)
    async def patchproof_get_capabilities() -> Capabilities:
        """Query supported inputs, resource limits, service/SDK versions and validation capabilities. No parameters. Detection does not validate PSD2UI one-pixel collapse."""
        return Capabilities.model_validate({'service':'patchproof-http','version':__version__,'inputs':['assetId'] + (['localPath'] if roots else []),
                'limits':{'maxItems':100,'maxUploadBytes':20971520,'maxPixels':4000000,'concurrency':1},
                'transport':'streamable-http','sdk':'mcp ' + package_version('mcp'),'collapseValidation':False})

    @mcp.tool(structured_output=True)
    async def patchproof_analyze(requestKey: Annotated[str, Field(min_length=1,max_length=256,description="Idempotency key: identical input reuses a job; changed input conflicts. Use a new key to retry interrupted jobs.")], items: Annotated[list[Item],Field(min_length=1,max_length=100,description="PNG resources with unique clientItemId and exactly one input source each.")], options: Annotated[Options | None, Field(description="Detection threshold and optional preview sizes.")] = None) -> JobResult:
        """Submit a persistent asynchronous analysis batch and return jobId, status and counters immediately. Poll patchproof_get_job for results; disconnects do not cancel jobs. Invalid images produce independent item errors. UNCERTAIN borders are null; PROVEN does not establish PSD2UI collapse safety."""
        if not requestKey or len(requestKey)>256:
            raise ValueError('requestKey must have 1–256 characters')
        if len({item.clientItemId for item in items}) != len(items):
            raise ValueError('Duplicate clientItemId')
        return JobResult.model_validate(service.submit({'requestKey':requestKey,'items':[i.model_dump(exclude_none=True) for i in items], 'options':(options or Options()).model_dump()}))

    @mcp.tool(structured_output=True)
    async def patchproof_get_job(jobId: Annotated[str, Field(description="Persistent job ID returned by analyze.")], offset: Annotated[int, Field(ge=0,description="Zero-based result offset.")] = 0, limit: Annotated[int, Field(ge=1,le=100,description="Maximum results in this page.")] = 20) -> JobPage:
        """Poll persisted job status and a page of completed item results. Counters describe the whole job. Poll until a terminal status; interrupted jobs require explicit resubmission with a new requestKey. Unknown job IDs are tool errors."""
        if offset<0 or not 1<=limit<=100:
            raise ValueError('Invalid pagination')
        job = service.public(service.get(jobId))
        job['results'] = job['results'][offset:offset+limit]
        job['offset'] = offset
        return JobPage.model_validate(job)

    @mcp.tool(structured_output=True)
    async def patchproof_cancel_job(jobId: Annotated[str, Field(description="Persistent job ID to cancel.")]) -> JobResult:
        """Cancel a queued or running job, preserving completed item results. Running jobs first return cancelling; poll get_job until cancelled. Terminal jobs are returned unchanged. Unknown job IDs are tool errors."""
        job = service.get(jobId)
        if job['status'] in ('queued','running'):
            job['status'] = 'cancelled' if job['status']=='queued' else 'cancelling'
            service.save(job)
        return JobResult.model_validate(service.public(job))

    @mcp.tool(structured_output=True)
    async def patchproof_get_preview(jobId: Annotated[str, Field(description="Job containing the completed item.")], clientItemId: Annotated[str, Field(description="Original caller resource identity from analyze.")]) -> PreviewResult:
        """Return one completed item's preview artifact IDs, detection and error metadata. Download images via downloadPrefix + artifact ID using the same Bearer token; no image bytes are embedded. Unavailable results are tool errors; uncertain items may have no previews."""
        job = service.get(jobId)
        for result in job['results']:
            if result['clientItemId'] == clientItemId:
                return PreviewResult.model_validate({'artifacts':result.get('artifacts',{}),'downloadPrefix':'/v1/artifacts/','detection':result.get('detection'),'error':result.get('error')})
        raise ValueError('Result not yet available')

    async def health(request):
        return JSONResponse({'service':'patchproof-http','status':'ok'})

    async def ready(request):
        service.db.execute('SELECT 1')
        ok = not request.app.state.worker.done()
        return JSONResponse({'ready':ok},status_code=200 if ok else 503)

    async def upload(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>20971520:
                return JSONResponse({'error':'Upload too large'},status_code=413)
        try:
            return JSONResponse(service.ingest(data),status_code=201)
        except Exception as exc:
            return JSONResponse({'error':str(exc)},status_code=400)

    async def artifact(request):
        path = (service.directory / request.path_params['id']).resolve()
        if not path.is_relative_to(service.directory.resolve()) or path.suffix != '.png' or len(path.relative_to(service.directory.resolve()).parts)!=3 or not path.is_file():
            return JSONResponse({'error':'Unknown artifact'},status_code=404)
        return FileResponse(path,media_type='image/png')

    @asynccontextmanager
    async def lifespan(app):
        app.state.worker = asyncio.create_task(service.run())
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                app.state.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await app.state.worker
                service.db.close()

    transport = mcp.streamable_http_app()
    app = Starlette(routes=[Route('/healthz',health),Route('/readyz',ready),Route('/v1/assets',upload,methods=['POST']),Route('/v1/artifacts/{id:path}',artifact),Mount('/',transport)],lifespan=lifespan)

    class Guard:
        async def __call__(self, scope, receive, send):
            if scope['type']=='http':
                headers = dict(scope['headers'])
                host = headers.get(b'host',b'').decode()
                origin = headers.get(b'origin')
                if host not in allowed_hosts or (origin and origin.decode() not in allowed_origins):
                    await JSONResponse({'error':'Host or Origin rejected'},status_code=403)(scope,receive,send)
                    return
                if scope['path']!='/healthz' and not secrets.compare_digest(headers.get(b'authorization',b''),('Bearer '+token).encode()):
                    await JSONResponse({'error':'Unauthorized'},status_code=401)(scope,receive,send)
                    return
                length = headers.get(b'content-length',b'0')
                if not length.isdigit() or int(length)>20971520:
                    await JSONResponse({'error':'Request too large'},status_code=413)(scope,receive,send)
                    return
            await app(scope,receive,send)
    return Guard()


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description='PatchProof HTTP MCP service')
    parser.add_argument('--data-dir',type=Path,default=Path.home()/'.patchproof-http')
    parser.add_argument('--allow-root',action='append',default=[])
    parser.add_argument('--port',type=int,default=18741)
    parser.add_argument('--host',default='127.0.0.1',help='Listen address; use 0.0.0.0 for remote access')
    parser.add_argument('--allowed-host',action='append',default=[],help='Additional accepted Host header, including port when present; repeatable')
    parser.add_argument('--allowed-origin',action='append',default=[],help='Additional accepted browser Origin (scheme://host[:port]); repeatable')
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    config = args.data_dir/'config.json'
    if not config.exists():
        with config.open('x') as stream:
            config.chmod(0o600)
            json.dump({'token':secrets.token_urlsafe(32)},stream)
    token = json.loads(config.read_text())['token']
    hosts = [f'127.0.0.1:{args.port}', f'localhost:{args.port}', *args.allowed_host]
    origins = [f'http://127.0.0.1:{args.port}', f'http://localhost:{args.port}', *args.allowed_origin]
    uvicorn.run(create_app(args.data_dir,args.allow_root,token,
                          allowed_hosts=hosts,allowed_origins=origins),
                host=args.host,port=args.port,workers=1)
