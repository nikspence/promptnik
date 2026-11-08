import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from promptnik import core
from promptnik.storage import Store

class Response(io.BytesIO):
    def __init__(self, data, ctype='application/json'):
        super().__init__(data); self.headers={'Content-Type':ctype}

class Contracts(unittest.TestCase):
    def test_overall_rate_requires_counts_and_positive_elapsed_time(self):
        self.assertEqual(core.total_token_rate({'input_tokens':80,'output_tokens':20,'total_seconds':4}),25)
        self.assertEqual(core.total_token_rate({'input_tokens':10,'output_tokens':0,'total_seconds':2}),5)
        self.assertEqual(core.total_token_rate({'input_tokens':0,'output_tokens':0,'total_seconds':2}),0)
        for metrics in ({'output_tokens':20,'total_seconds':4},{'input_tokens':10,'output_tokens':20,'total_seconds':0},{}):
            self.assertIsNone(core.total_token_rate(metrics))
    def test_images_and_history_each_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            a=Path(root)/'a.png'; a.write_bytes(b'first'); b=Path(root)/'b.jpg'; b.write_bytes(b'second')
            for protocol in core.PROTOCOLS:
                c={'url':'https://test/v1','model':'vision','protocol':protocol}
                body=core.chat_body(c,[{'role':'user','text':'compare','images':[str(a),str(b)]},{'role':'assistant','text':'answer'}])
                history=body['input' if protocol=='responses' else 'messages']
                self.assertEqual(len(history[0]['content']),3)
                self.assertEqual(history[1]['role'],'assistant')
                self.assertEqual(body['model'],'vision')

    def test_decision_mixed_questions_and_multiple_images(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'image.png'; p.write_bytes(b'image')
            form={'state':'{"ticket":"broken"}','json_state':True,'images':[str(p),str(p)],'questions':[
                {'name':'yes','type':'noul','instructions':'Is it broken?'},
                {'name':'label','type':'choice','instructions':'Which?','choices':[['bug','Software bug'],['other','Other']]},
                {'name':'score','type':'score','instructions':'Urgency?','choices':[['','Low'],['','High']]}]}
            body=core.decision_body({'model':'clef'},form)
            self.assertEqual(body['state'],{'ticket':'broken'})
            self.assertEqual(list(body['questions']),['yes','label','score'])
            self.assertEqual(body['questions']['score']['criteria'],['Low','High'])
            self.assertEqual(body['images'],['aW1hZ2U=','aW1hZ2U='])
            form['questions'].append(dict(form['questions'][0]))
            with self.assertRaises(ValueError): core.decision_body({'model':'clef'},form)

    def test_image_only_decision_supplies_required_state(self):
        with tempfile.TemporaryDirectory() as root:
            image=Path(root)/'a.png'; image.write_bytes(b'image')
            form={'state':'','json_state':True,'images':[str(image)],'questions':[{'name':'q','type':'noul','instructions':'Is there a vehicle?'}]}
            body=core.decision_body({'model':'test'},form)
            self.assertEqual(body['state'],'Evaluate the attached images.')
            self.assertEqual(len(body['images']),1)
            form['images']=[]
            with self.assertRaises(ValueError): core.decision_body({'model':'test'},form)

    def test_stream_protocols_and_usage(self):
        fixtures = [
            ('chat', [{'choices':[{'delta':{'content':'Hello '}}]},{'choices':[{'delta':{'content':'world'}}]},{'usage':{'prompt_tokens':15,'completion_tokens':2},'choices':[]}]),
            ('responses',[{'type':'response.output_text.delta','delta':'Hello world'},{'type':'response.completed','response':{'usage':{'input_tokens':15,'output_tokens':2}}}]),
            ('messages',[{'type':'message_start','message':{'usage':{'input_tokens':15}}},{'type':'content_block_delta','delta':{'text':'Hello world'}},{'type':'message_delta','usage':{'output_tokens':2}}]),
        ]
        for protocol, objects in fixtures:
            wire=''.join('data: '+json.dumps(o)+'\n\n' for o in objects)+'data: [DONE]\n\n'
            with patch.object(core,'open_request',return_value=Response(wire.encode(),'text/event-stream')):
                progress=[]
                result=core.run_request({'url':'https://test/v1','protocol':protocol},'chat',{},progress.append,threading.Event())
            self.assertEqual(result['text'],'Hello world'); self.assertEqual(result['metrics']['input_tokens'],15); self.assertEqual(result['metrics']['output_tokens'],2)
            self.assertNotIn('thinking_seconds_estimate',result['metrics']); self.assertNotIn('generation_tps',result['metrics'])

    def test_ollama_metrics_and_request(self):
        c={'url':'http://localhost:11434/v1','protocol':'chat','model':'local','context_limit':8192}
        self.assertEqual(core.endpoint(c),'http://localhost:11434/api/chat')
        body=core.chat_body(c,[{'role':'user','text':'hello'}]); self.assertEqual(body['options']['num_ctx'],8192)
        raw={'message':{'content':'answer'},'prompt_eval_count':100,'eval_count':10,'prompt_eval_duration':500000000,'eval_duration':2000000000}
        with patch.object(core,'open_request',return_value=Response((json.dumps(raw)+'\n').encode(),'application/x-ndjson')), patch.object(core,'get_json',return_value={'models':[{'name':'local:latest','context_length':8192}]}):
            result=core.run_request(c,'chat',body,lambda _:None,threading.Event())
        self.assertEqual(result['text'],'answer'); self.assertEqual(result['metrics']['prompt_tps'],200); self.assertEqual(result['metrics']['generation_tps'],5)
        self.assertEqual(result['metrics']['context_limit'],8192)

    def test_nonstreaming_responses(self):
        for protocol, raw in [
            ('chat',{'choices':[{'message':{'content':'ok'}}],'usage':{'prompt_tokens':4,'completion_tokens':1}}),
            ('responses',{'output':[{'content':[{'type':'output_text','text':'ok'}]}],'usage':{'input_tokens':4,'output_tokens':1}}),
            ('messages',{'content':[{'type':'text','text':'ok'}],'usage':{'input_tokens':4,'output_tokens':1}})]:
            with patch.object(core,'open_request',return_value=Response(json.dumps(raw).encode())):
                result=core.run_request({'url':'https://test/v1','protocol':protocol},'chat',{},lambda _:None,threading.Event())
            self.assertEqual(result['text'],'ok'); self.assertEqual(result['metrics']['input_tokens'],4)

    def test_cancel_and_api_error(self):
        cancel=threading.Event(); cancel.set()
        with patch.object(core,'open_request',return_value=Response(b'{"choices":[]}')):
            with self.assertRaises(InterruptedError): core.run_request({'url':'https://test'},'chat',{},lambda _:None,cancel)
        with patch.object(core,'open_request',return_value=Response(b'{"error":"unsupported image"}')):
            with self.assertRaisesRegex(ValueError,'unsupported image'): core.run_request({'url':'https://test'},'chat',{},lambda _:None,threading.Event())

    def test_endpoint_and_config_validation(self):
        self.assertEqual(core.base_url('localhost:4000'),'http://localhost:4000/v1')
        self.assertEqual(core.base_url('https://example/api/v1/chat/completions'),'https://example/api/v1')
        self.assertEqual(core.endpoint({'url':'http://localhost:11434','protocol':'chat'},'decision'),'http://localhost:11434/v1/systemone')
        with self.assertRaises(ValueError): core.base_url('https://secret@example.com')
        with self.assertRaises(ValueError): core.validate_connections({'connections':{'bad':{'url':'https://test','context_limit':'bad'}}})

    def test_environment_wildcard_addresses(self):
        cases = [
            ('OLLAMA_HOST','0.0.0.0:11434','env:Ollama','127.0.0.1:11434'),
            ('OLLAMA_BASE_URL','http://0.0.0.0:11434/v1','env:Ollama','http://127.0.0.1:11434/v1'),
            ('LITELLM_URL','https://0.0.0.0:4000/proxy/v1','env:LiteLLM','https://127.0.0.1:4000/proxy/v1'),
            ('BEDROCK_MANTLE_URL','http://0.0.0.0:8080/v1','env:Bedrock Mantle','http://127.0.0.1:8080/v1'),
            ('OPENAI_BASE_URL','http://example.com/0.0.0.0','env:OpenAI','http://example.com/0.0.0.0'),
            ('OLLAMA_HOST','192.168.1.2:11434','env:Ollama','192.168.1.2:11434'),
        ]
        for variable,value,name,expected in cases:
            with self.subTest(variable=variable,value=value), patch.dict('os.environ',{variable:value},clear=True):
                connection=core.environment_connections()[name]
                self.assertEqual(connection['url'],expected)
                if value=='0.0.0.0:11434':
                    self.assertEqual(core.endpoint(connection),'http://127.0.0.1:11434/api/chat')
                self.assertEqual(os.environ[variable],value)

    def test_discovery_and_environment(self):
        with patch.dict('os.environ',{'AWS_BEARER_TOKEN_BEDROCK':'key','AWS_REGION':'us-east-1','OLLAMA_HOST':'localhost:11434'},clear=True):
            c=core.environment_connections(); self.assertEqual(c['env:Bedrock Mantle']['url'],'https://bedrock-mantle.us-east-1.api.aws/v1'); self.assertIn('env:Ollama',c)
        def connect(address,timeout):
            if address[1] != 4000: raise OSError()
            return unittest.mock.MagicMock()
        with patch.object(core.socket,'create_connection',side_effect=connect), patch.object(core,'models',side_effect=ValueError('HTTP 401')):
            found=core.discover()
            self.assertEqual(len(found),1); self.assertIn('authentication required',next(iter(found)))

    def test_discovery_merges_environment_loopback_ports(self):
        env={'env:Ollama':{'url':'http://localhost:11434/v1','api_key':'test','model':'chosen'}}
        local={'local:Ollama':{'url':'http://127.0.0.1:11434/v1','model':'other'},'local:Other':{'url':'http://127.0.0.1:4000/v1'}}
        merged=core.merge_discovered_connections(env,local)
        self.assertEqual(set(merged),{'env:Ollama','local:Other'})
        self.assertEqual(merged['env:Ollama'],env['env:Ollama'])
        for host in ('[::1]','0.0.0.0'):
            local['local:Ollama']['url']=f'http://{host}:11434/v1'
            self.assertNotIn('local:Ollama',core.merge_discovered_connections(env,local))
        env['env:Ollama']['url']='http://192.168.1.2:11434/v1'
        self.assertIn('local:Ollama',core.merge_discovered_connections(env,local))

class History(unittest.TestCase):
    def test_saved_prompts_keep_shared_images_until_unreferenced(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root); image=store.attach(data=b'image')
            session=store.new_session('chat','test',{'url':'http://localhost:11434/v1'})
            session['messages']=[{'role':'user','text':'Compare','images':[image]}]; store.save(session)
            ident=store.save_prompt('chat',session['messages'][0]); self.assertEqual(ident,store.save_prompt('chat',session['messages'][0]))
            store.delete([session['id']]); self.assertTrue(Path(image).exists())
            store.db.close(); store=Store(root)
            self.assertEqual(store.list_prompts('chat')[0]['payload']['text'],'Compare')
            store.delete_prompt(ident); self.assertFalse(Path(image).exists()); store.db.close()

    def test_history_and_shared_attachment_collection(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root); c={'url':'http://localhost:11434/v1','api_key':'do-not-persist','model':'vision'}
            image=store.attach(data=b'image')
            a=store.new_session('chat','local',c); a['draft_images']=[image]; store.save(a)
            b=store.new_session('decision','local',c); b['form']['images']=[image]; store.save(b)
            self.assertNotIn('do-not-persist',str(store.list_sessions()))
            store.delete([a['id']]); self.assertTrue(Path(image).exists())
            store.delete([b['id']]); self.assertFalse(Path(image).exists()); store.db.close()

    def test_saved_connection_plaintext_opt_in_and_keyring_default(self):
        import sys
        fake=unittest.mock.MagicMock(); fake.get_password.return_value='secret'
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules,{'keyring':fake}):
            store=Store(root); c={'url':'https://test/v1','api_key':'secret'}
            store.save_connection('test',c); self.assertNotIn('secret',store.config_path.read_text()); self.assertEqual(store.connections()['test']['api_key'],'secret')
            fake.set_password.side_effect=RuntimeError('unavailable')
            with self.assertRaises(RuntimeError): store.save_connection('test',{'url':'https://changed','api_key':'replacement'})
            self.assertEqual(store.connections()['test']['url'],c['url'])
            store.save_connection('plain',c,True); self.assertEqual(store.config['connections']['plain']['api_key'],'secret'); store.db.close()

if __name__=='__main__': unittest.main()
