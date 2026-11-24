import asyncio
import io
import time
from PIL import Image
from starlette.testclient import TestClient
from patchproof.mcp.app import create_app
from patchproof.mcp.service import Service


def png():
    data = io.BytesIO()
    Image.new('RGBA',(32,32),'red').save(data,format='PNG')
    return data.getvalue()


def test_http_security_and_mcp(tmp_path):
    with TestClient(create_app(tmp_path,token='test'),base_url='http://localhost:18741') as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 401
        headers = {'Authorization':'Bearer test'}
        assert client.get('/readyz',headers=headers).status_code == 200
        assert client.get('/readyz',headers={**headers,'Origin':'https://evil.com'}).status_code == 403
        assert client.post('/v1/assets',content=png(),headers=headers).status_code == 201
        assert client.post('/v1/assets',content=b'bad',headers=headers).status_code == 400
        headers['Accept'] = 'application/json, text/event-stream'
        response = client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        assert 'result' in response.json(), response.text
        response = client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
        assert len(response.json()['result']['tools']) == 5


def test_jobs(tmp_path):
    async def scenario():
        service = Service(tmp_path)
        asset = service.ingest(png())
        request = {'requestKey':'one','items':[{'clientItemId':'panel','assetId':asset['assetId']}],'options':{'threshold':6,'previewSizes':[{'width':64,'height':64}]}}
        submitted = service.submit(request)
        assert service.submit(request)['jobId']==submitted['jobId']
        task = asyncio.create_task(service.run())
        try:
            for _ in range(400):
                job = service.get(submitted['jobId'])
                if job['status'].startswith('completed'):
                    break
                await asyncio.sleep(.05)
            assert job['status']=='completed', job
            assert job['results'][0]['detection']['verdict']=='PROVEN'
            assert (tmp_path/job['results'][0]['artifacts']['borderPreviewId']).is_file()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            service.db.close()
    asyncio.run(scenario())
