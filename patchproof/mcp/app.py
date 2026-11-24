"""Local authenticated Streamable HTTP host (MCP SDK 1.x)."""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import secrets
from typing import Annotated
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Mount, Route
from .service import Service


class Item(BaseModel):
    clientItemId: str = Field(min_length=1,max_length=256)
    assetId: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    localPath: str | None = None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.assetId is None) == (self.localPath is None):
            raise ValueError('Exactly one of assetId and localPath is required')
        return self


class Size(BaseModel):
    width: int = Field(ge=1,le=2048)
    height: int = Field(ge=1,le=2048)


class Options(BaseModel):
    threshold: float = Field(default=6,ge=0,le=255,allow_inf_nan=False)
    previewSizes: list[Size] = Field(default_factory=list,max_length=4)


def create_app(directory, roots=(), token=None, timeout=60):
    service = Service(directory, roots, timeout)
    if not token:
        raise ValueError('An access token is required')
    mcp = FastMCP('PatchProof',stateless_http=True,json_response=True)

    @mcp.tool()
    async def patchproof_get_capabilities() -> dict:
        return {'service':'patchproof-http','version':'0.1.0','inputs':['assetId'] + (['localPath'] if roots else []),
                'limits':{'maxItems':100,'maxUploadBytes':20971520,'maxPixels':4000000,'concurrency':1},
                'transport':'streamable-http','sdk':'mcp 1.26.0','collapseValidation':False}

    @mcp.tool()
    async def patchproof_analyze(requestKey: str, items: Annotated[list[Item],Field(min_length=1,max_length=100)], options: Options | None = None) -> dict:
        if not requestKey or len(requestKey)>256:
            raise ValueError('requestKey must have 1–256 characters')
        if len({item.clientItemId for item in items}) != len(items):
            raise ValueError('Duplicate clientItemId')
        return service.submit({'requestKey':requestKey,'items':[i.model_dump(exclude_none=True) for i in items], 'options':(options or Options()).model_dump()})

    @mcp.tool()
    async def patchproof_get_job(jobId: str, offset: int = 0, limit: int = 20) -> dict:
        if offset<0 or not 1<=limit<=100:
            raise ValueError('Invalid pagination')
        job = service.public(service.get(jobId))
        job['results'] = job['results'][offset:offset+limit]
        job['offset'] = offset
        return job

    @mcp.tool()
    async def patchproof_cancel_job(jobId: str) -> dict:
        job = service.get(jobId)
        if job['status'] in ('queued','running'):
            job['status'] = 'cancelled' if job['status']=='queued' else 'cancelling'
            service.save(job)
        return service.public(job)

    @mcp.tool()
    async def patchproof_get_preview(jobId: str, clientItemId: str) -> dict:
        job = service.get(jobId)
        for result in job['results']:
            if result['clientItemId'] == clientItemId:
                return {'artifacts':result.get('artifacts',{}),'downloadPrefix':'/v1/artifacts/','detection':result.get('detection'),'error':result.get('error')}
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
                host = headers.get(b'host',b'').decode().split(':')[0]
                origin = headers.get(b'origin')
                if host not in ('127.0.0.1','localhost') or (origin and origin.decode() not in ('http://127.0.0.1:18741','http://localhost:18741')):
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
    parser = argparse.ArgumentParser(description='PatchProof local HTTP MCP service')
    parser.add_argument('--data-dir',type=Path,default=Path.home()/'.patchproof-http')
    parser.add_argument('--allow-root',action='append',default=[])
    parser.add_argument('--port',type=int,default=18741)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    config = args.data_dir/'config.json'
    if not config.exists():
        with config.open('x') as stream:
            config.chmod(0o600)
            json.dump({'token':secrets.token_urlsafe(32)},stream)
    token = json.loads(config.read_text())['token']
    uvicorn.run(create_app(args.data_dir,args.allow_root,token),host='127.0.0.1',port=args.port,workers=1)
