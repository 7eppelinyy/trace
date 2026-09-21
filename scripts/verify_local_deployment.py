"""Portable two-process startup acceptance; no credentials, external calls or deliveries.

The maintenance profile deliberately disables external collection/quotes/digests.
It proves process startup, production auth, shared migrations/storage and worker heartbeat,
not live supplier ingestion, container execution, or production availability.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


def verify():
    result = {'profile':'production-maintenance', 'real_external_calls':False, 'container_verified':False}
    with tempfile.TemporaryDirectory(prefix='trace-process-acceptance-',ignore_cleanup_errors=True) as temp:
        directory = Path(temp)
        cfg = yaml.safe_load((ROOT/'trace/settings.yaml').read_text(encoding='utf-8'))
        cfg['database']['path'] = str(directory/'shared.db')
        cfg['embedding']['provider'] = 'hash'
        cfg['collectors']['enabled'] = False
        cfg['markets']['providers_enabled'] = False
        for section in ('digest','forecast','backup'):
            cfg[section]['enabled'] = False
        config_path = directory/'settings.yaml'
        config_path.write_text(yaml.safe_dump(cfg),encoding='utf-8')
        env = {k:v for k,v in os.environ.items() if not k.startswith(('TRACE_','OPENAI_','GEMINI_','ALPACA_','TELEGRAM_','JIN10_','LLM_'))}
        env.update(TRACE_MODE='production',TRACE_ALLOW_DEV_AUTH='0',TRACE_LOAD_DOTENV='0',TRACE_SETTINGS_PATH=str(config_path),
                   PYTHONIOENCODING='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        flags = getattr(subprocess,'CREATE_NO_WINDOW',0)
        processes = []
        with (directory/'api.log').open('w',encoding='utf-8') as api_log, (directory/'runner.log').open('w',encoding='utf-8') as runner_log:
            try:
                processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','trace.api.app:app','--host','127.0.0.1','--port',str(port)],
                    cwd=ROOT,env=env,stdout=api_log,stderr=subprocess.STDOUT,creationflags=flags))
                processes.append(subprocess.Popen([sys.executable,'-m','trace.main','run'],cwd=ROOT,env=env,
                    stdout=runner_log,stderr=subprocess.STDOUT,creationflags=flags))
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=2) as client:
                    deadline = time.monotonic()+40
                    while time.monotonic()<deadline:
                        if any(p.poll() is not None for p in processes):
                            raise RuntimeError('API or runner terminated before readiness')
                        try:
                            ready = client.get('/ready')
                            if ready.status_code==200: break
                        except httpx.HTTPError: pass
                        time.sleep(.1)
                    else: raise RuntimeError('Startup timed out')
                    assert client.post('/api/v1/auth/session',json={'grant_type':'dev','user_id':'victim'}).status_code == 403
                    login = client.post('/api/v1/auth/session',json={'grant_type':'guest'})
                    assert login.status_code == 200
                    token = login.json()['session_token']
                    headers = {'Authorization':'Bearer '+token}
                    question = client.post('/api/v1/research/questions',headers=headers,
                        json={'title':'Private smoke fixture','hypothesis':'A conditional question'})
                    assert question.status_code == 200
                    qid = question.json()['question_id']
                    assert client.get(f'/api/v1/research/questions/{qid}/share').status_code == 404
                    assert client.get('/api/v1/research/export',headers=headers).json()['questions'][0]['question_id'] == qid
                    assert client.get('/status').status_code == 401
                    result['http_contracts'] = 'passed'
                    result['readiness'] = ready.json()
                    while time.monotonic()<deadline:
                        with sqlite3.connect(directory/'shared.db') as db:
                            count = db.execute('SELECT COUNT(*) FROM run_history').fetchone()[0]
                        if count: break
                        time.sleep(.1)
                    assert count > 0
                    assert all(p.poll() is None for p in processes)
                    result['runner_completed_rounds'] = count
                    result['shared_storage_and_processes'] = 'passed'
            finally:
                for process in processes:
                    if process.poll() is None:
                        if os.name == 'nt':
                            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=flags,timeout=10)
                        else:
                            process.terminate()
                for process in processes:
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                with socket.socket() as check:
                    check.settimeout(1)
                    if check.connect_ex(('127.0.0.1',port)) == 0:
                        raise RuntimeError('Acceptance server did not shut down')
    return result


if __name__ == '__main__':
    output = verify()
    target = ROOT/'verification/product-hardening/local-process-smoke.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    next_pass_target = ROOT/'verification/product-hardening/next-pass/local-process-smoke.json'
    next_pass_target.parent.mkdir(parents=True,exist_ok=True)
    next_pass_target.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False,indent=2))
