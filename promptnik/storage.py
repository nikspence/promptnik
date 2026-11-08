"""Local history and credentials. Sessions never contain API keys."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

def default_home():
    if os.getenv('PROMPTNIK_HOME'): return Path(os.environ['PROMPTNIK_HOME'])
    if sys.platform == 'win32': return Path(os.getenv('LOCALAPPDATA', Path.home()/'AppData/Local'))/'Promptnik'
    if sys.platform == 'darwin': return Path.home()/'Library/Application Support/Promptnik'
    return Path(os.getenv('XDG_DATA_HOME', Path.home()/'.local/share'))/'promptnik'

def safe_connection(c):
    return {k:v for k,v in c.items() if k not in ('api_key', 'credential_id')}

class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_home()
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments = self.root/'attachments'; self.attachments.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root/'history.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, updated REAL, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, mode TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL, UNIQUE(mode, payload))')
        self.config_path = self.root/'connections.json'
        self.config = json.loads(self.config_path.read_text('utf-8')) if self.config_path.exists() else {'connections':{}, 'settings':{}}
        self.config.setdefault('settings', {})

    def flush(self):
        temp = self.config_path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.config, indent=2)+'\n', encoding='utf-8')
        temp.replace(self.config_path)

    def connections(self):
        import keyring
        result = {}
        for name, c in self.config['connections'].items():
            result[name] = dict(c)
            if c.get('credential_id'):
                try: result[name]['api_key'] = keyring.get_password('Promptnik', c['credential_id']) or ''
                except Exception: result[name]['api_key'] = ''
        return result

    def save_connection(self, name, c, plaintext=False):
        import keyring
        old = self.config['connections'].get(name, {})
        saved = safe_connection(c)
        if c.get('api_key'):
            if plaintext: saved['api_key'] = c['api_key']
            else:
                credential_id = str(uuid.uuid4())
                # Save the secret first; failure leaves the old entry intact.
                keyring.set_password('Promptnik', credential_id, c['api_key'])
                saved['credential_id'] = credential_id
        self.config['connections'][name] = saved
        self.flush()
        if old.get('credential_id'):
            try: keyring.delete_password('Promptnik', old['credential_id'])
            except Exception: pass

    def delete_connection(self, name):
        c = self.config['connections'].pop(name)
        self.flush()
        if c.get('credential_id'):
            import keyring
            try: keyring.delete_password('Promptnik', c['credential_id'])
            except Exception: pass

    def new_session(self, mode, name, c):
        return dict(id=str(uuid.uuid4()), title='New decision' if mode == 'decision' else 'New chat', mode=mode,
                    connection_name=name, connection=safe_connection(c), messages=[], runs=[],
                    form={'state':'', 'json_state':False, 'questions':[], 'images':[]}, draft='', draft_images=[])

    def save(self, session):
        data = dict(session); data['connection'] = safe_connection(data['connection'])
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)', (data['id'], time.time(), json.dumps(data)))
        self.db.commit()

    def list_sessions(self):
        return [dict(json.loads(payload), updated=updated) for updated,payload in self.db.execute('SELECT updated, payload FROM sessions ORDER BY updated DESC')]

    @staticmethod
    def prompt_json(mode, payload):
        if mode == 'chat':
            data = {'text':payload.get('text',''), 'images':list(payload.get('images',[]))}
        elif mode == 'decision':
            data = {'state':payload.get('state',''), 'json_state':bool(payload.get('json_state',False)), 'questions':payload.get('questions',[]), 'images':list(payload.get('images',[]))}
        else: raise ValueError('Invalid prompt type')
        return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))

    def prompt_id(self, mode, payload):
        row = self.db.execute('SELECT id FROM prompts WHERE mode=? AND payload=?',(mode,self.prompt_json(mode,payload))).fetchone()
        return row[0] if row else None

    def save_prompt(self, mode, payload):
        data = self.prompt_json(mode,payload); existing = self.prompt_id(mode,payload)
        if existing: return existing
        ident = str(uuid.uuid4())
        self.db.execute('INSERT INTO prompts VALUES (?, ?, ?, ?)',(ident,mode,time.time(),data)); self.db.commit()
        return ident

    def list_prompts(self, mode=None):
        if mode:
            rows = self.db.execute('SELECT id,mode,payload FROM prompts WHERE mode=? ORDER BY created DESC',(mode,))
        else: rows = self.db.execute('SELECT id,mode,payload FROM prompts ORDER BY created DESC')
        return [dict(id=ident,mode=kind,payload=json.loads(payload)) for ident,kind,payload in rows]

    def delete_prompt(self, ident):
        self.db.execute('DELETE FROM prompts WHERE id=?',(ident,)); self.db.commit(); self.collect_attachments()

    def delete(self, ids):
        self.db.executemany('DELETE FROM sessions WHERE id=?', [(x,) for x in ids]); self.db.commit()
        self.collect_attachments()

    def collect_attachments(self):
        used = set()
        def walk(value):
            if isinstance(value, str): used.add(value)
            elif isinstance(value, list):
                for x in value: walk(x)
            elif isinstance(value, dict):
                for x in value.values(): walk(x)
        for session in self.list_sessions(): walk(session)
        for prompt in self.list_prompts(): walk(prompt)
        for p in self.attachments.iterdir():
            if str(p) not in used and p.is_file(): p.unlink()

    def attach(self, path=None, data=None, suffix='.png'):
        if path:
            path = Path(path); suffix = path.suffix.lower(); data = path.read_bytes()
        if not data: raise ValueError('Empty image')
        target = self.attachments/(hashlib.sha256(data).hexdigest()+suffix)
        if not target.exists(): target.write_bytes(data)
        return str(target)
