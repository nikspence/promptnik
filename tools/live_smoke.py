"""Explicit live checks. Keys are read from files, never printed or persisted."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from promptnik import core

def main():
    p=argparse.ArgumentParser(); p.add_argument('--litellm-key-file',type=Path); p.add_argument('--ollama-key-file',type=Path); p.add_argument('--generate',action='store_true'); p.add_argument('--decision-model'); p.add_argument('--image',action='append',default=[]); args=p.parse_args()
    connections=[('Local Ollama',{'url':'http://localhost:11434/v1','protocol':'chat','model':''})]
    if args.litellm_key_file: connections.append(('LiteLLM',{'url':'https://gchat.glovis.app:4000/v1','protocol':'chat','api_key':args.litellm_key_file.read_text('utf-8-sig').strip()}))
    if args.ollama_key_file: connections.append(('Ollama Cloud',{'url':'https://ollama.com/v1','protocol':'chat','api_key':args.ollama_key_file.read_text('utf-8-sig').strip()}))
    for name,c in connections:
        try:
            names=core.models(c); print(name+': models '+json.dumps(names))
            if args.generate and names:
                # Prefer a small general chat model when available; decision models are tested separately.
                candidates=[n for n in names if not any(x in n.lower() for x in ('embed','clef','nimble','tev','jev','d1:','fovea','systemone'))]
                if not candidates: print(name+': no general chat model selected'); continue
                c['model']=next((n for n in candidates if any(x in n.lower() for x in ('gemma3:1b','llama3.2:1b','gpt-4o-mini','gpt-4.1-mini'))),candidates[0])
                result=core.run_request(c,'chat',core.chat_body(c,[{'role':'user','text':'Reply with exactly: Promptnik connected','images':args.image}]),lambda _:None,threading.Event())
                print(name+': chat '+json.dumps({'model':c['model'],'text':result['text'],'metrics':result['metrics']}))
            if args.decision_model and name=='Local Ollama':
                c['model']=args.decision_model
                form={'state':'A customer reports that checkout returns a server error.','images':args.image,'questions':[{'name':'bug','type':'noul','instructions':'Is a software error reported?'},{'name':'route','type':'choice','instructions':'Which team should handle this?','choices':[['engineering','Software errors'],['billing','Payments and refunds']]}]}
                result=core.run_request(c,'decision',core.decision_body(c,form),lambda _:None,threading.Event()); print(name+': decision '+json.dumps(result['raw']))
        except Exception as exc:
            message=str(exc)
            if c.get('api_key'): message=message.replace(c['api_key'],'[redacted]')
            print(name+': FAILED '+message)

if __name__=='__main__': main()
