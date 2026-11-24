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


def test_tool_contracts_and_versions(tmp_path):
    import json
    from importlib.metadata import version
    from jsonschema import validate
    from patchproof import __version__

    with TestClient(create_app(tmp_path, token='test'), base_url='http://localhost:18741') as client:
        headers = {'Authorization':'Bearer test', 'Accept':'application/json, text/event-stream'}

        def rpc(method, params):
            response = client.post('/mcp', headers=headers, json={
                'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params})
            assert response.status_code == 200, response.text
            return response.json()['result']

        initialized = rpc('initialize', {'protocolVersion':'2025-11-25', 'capabilities':{},
                                       'clientInfo':{'name':'contracts','version':'1'}})
        assert initialized['serverInfo']['version'] == __version__
        tools = {tool['name']:tool for tool in rpc('tools/list', {})['tools']}
        for tool in tools.values():
            assert tool['description'].strip()
            assert tool['outputSchema']['type'] == 'object'
            for parameter in tool['inputSchema'].get('properties', {}).values():
                assert parameter.get('description'), parameter

        def call(name, arguments):
            result = rpc('tools/call', {'name':name, 'arguments':arguments})
            assert not result.get('isError'), result
            structured = result['structuredContent']
            validate(structured, tools[name]['outputSchema'])
            assert json.loads(result['content'][0]['text']) == structured
            return structured

        capabilities = call('patchproof_get_capabilities', {})
        assert capabilities['version'] == initialized['serverInfo']['version']
        assert capabilities['sdk'] == 'mcp ' + version('mcp')
        asset = client.post('/v1/assets', headers=headers, content=png()).json()
        job = call('patchproof_analyze', {'requestKey':'contracts', 'items':[
            {'clientItemId':'panel', 'assetId':asset['assetId']},
            {'clientItemId':'missing', 'assetId':'0'*32}]})
        for _ in range(200):
            page = call('patchproof_get_job', {'jobId':job['jobId']})
            if page['status'] == 'completed_with_errors':
                break
            time.sleep(.05)
        assert page['status'] == 'completed_with_errors', page
        assert page['results'][0]['detection']['verdict'] == 'PROVEN'
        assert page['results'][1]['error']['code'] == 'INVALID_INPUT'
        call('patchproof_get_preview', {'jobId':job['jobId'], 'clientItemId':'panel'})
        call('patchproof_get_preview', {'jobId':job['jobId'], 'clientItemId':'missing'})
        call('patchproof_cancel_job', {'jobId':job['jobId']})
        failed = rpc('tools/call', {'name':'patchproof_get_job', 'arguments':{'jobId':'unknown'}})
        assert failed['isError'] is True
