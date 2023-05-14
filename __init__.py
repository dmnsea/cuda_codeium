#import sys
#sys.stdout = sys.__stdout__
#sys.stderr = sys.__stderr__

import sys
import os
import json
import gzip
import io
import queue
import subprocess
from threading import Thread
import requests
import shutil
import traceback
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
import uuid

from cudatext import *
import cudax_lib as apx
import cudatext_cmd as cmds
import cudatext_keys as keys

PLUGIN_NAME = 'cuda_codeium'
LOG = False

IS_WIN = os.name=='nt'
_MAXLINE = 65536

API_URL = 'https://server.codeium.com'
HEADERS_JSON            = { 'Content-Type': 'application/json' }
HEADERS_CONNECT_PROTO   = {
    'Content-Type': 'application/connect+proto',
    'connect-protocol-version': '1',
    #'connect-accept-encoding': 'gzip,br',
    #'Connection': 'close',
    #'Transfer-Encoding': 'chunked',
}
SNIP_ID = PLUGIN_NAME+'__snip'

if sys.platform == 'linux' and 'arm' in sys.version.lower():
    BIN_SUFFIX = 'linux_arm'
elif sys.platform == 'linux':
    BIN_SUFFIX = 'linux_x64'
elif sys.platform == 'darwin' and 'arm' in sys.version.lower():
    BIN_SUFFIX = 'macos_arm'
elif sys.platform == 'darwin':
    BIN_SUFFIX = 'macos_x64'
else:
    BIN_SUFFIX = 'windows_x64.exe'

fn_config = os.path.join(app_path(APP_DIR_SETTINGS), PLUGIN_NAME+'.ini')
option_token = ''
option_api_key = ''


Item = namedtuple('Item', 'hint text suffix text_inline text_inline_mask text_block start_position end_position cursor_offset')

SESSION_ID = str(uuid.uuid4())

class Command:
    
    def __init__(self):
        self.name = 'codeium'
        self.port = None
        self.token = None
        self.api_key = None
        self.language_server_version = '1.2.15'
        self.manager_dir = None
        self.text = '# print hello world in nim language\n'
        self.row = 1
        self.col = 0
        self.process = None
        self.caret_view = None
        
        global option_token
        global option_api_key
        option_token = ini_read(fn_config, 'op', 'token', option_token)
        option_api_key = ini_read(fn_config, 'op', 'api_key', option_api_key)
        self.token = option_token
        self.api_key = option_api_key
        
        self.conversations = {}
        self.in_process_of_creating_new_tab = False
        self.messages = []
        
    def get_token(self):
        url = 'https://www.codeium.com/profile?response_type=token&redirect_uri=vim-show-auth-token&state=a&scope=openid+profile+email&redirect_parameters_type=query'
        apx.safe_open_url(url)
        self.token = dlg_input('Your token: ', '')
        
        # save token to .ini
        global option_token
        option_token = self.token
        ini_write(fn_config, 'op', 'token', option_token)
        
        self.log_in()
        
    def register_user(self, token):
        #url = 'https://api.codeium.com/register_user/'
        url = API_URL + '/exa.api_server_pb.ApiServerService/RegisterUser'
        data = '{{"firebase_id_token": "{}"}}'.format(token)
        
        try:
            response = requests.post(url, headers=HEADERS_JSON, data=data, timeout=4)
            #response.raise_for_status()
        except requests.exceptions.Timeout:
            pass;      LOG and print("ERROR: Can't get API key: The request timed out.")
            return
        except requests.exceptions.RequestException as e:
            pass;      LOG and print("ERROR: Can't get API key. Error:", e)
            return
        result = response.content
        result_str = result.decode('utf-8')
        result_json = json.loads(result_str)
        
        api_key = result_json.get('api_key', None)
        if api_key is None:
            pass;      LOG and print("ERROR: Can't get API key..")
            return
        
        pass;    LOG and print("got api_key:", api_key)
        return api_key
        
    def download_server(self, out_file):
        url = "https://github.com/Exafunction/codeium/releases/download/language-server-v{}/language_server_{}.gz".format(
            self.language_server_version,
            BIN_SUFFIX
        )
        
        msg_status('{}: Downloading server...'.format(self.name), process_messages=True)
        response = requests.get(url)
        
        if response.status_code == 200:
            buffer = io.BytesIO(response.content)
            
            with gzip.GzipFile(fileobj=buffer, mode="rb") as gz_file:
                with open(out_file, "wb") as f_out:
                    shutil.copyfileobj(gz_file, f_out)            
            
            pass;    LOG and print("Codeium lang server downloaded!")
        else:
            print("ERROR: Cannot download Codeium lang server: {} - {}".format(response.status_code, response.reason))
            return
        
    def get_executable(self):
        data_dir = app_path(APP_DIR_DATA)
        codeium_dir = os.path.join(data_dir, PLUGIN_NAME)
        os.makedirs(codeium_dir, exist_ok=True)
        
        executable = os.path.join(codeium_dir,'language_server_'+BIN_SUFFIX)
        return executable
        
    def log_in(self):
        if self.process is not None:
            self.find_port()
            return
        
        msg_status('{}: Starting...'.format(self.name))
        
        if self.token is None:
            self.get_token()
        
        if not self.api_key:
            with ThreadPoolExecutor() as ex:
                future = ex.submit(self.register_user, self.token)
                while not future.done():
                    app_idle()
                    time.sleep(0.001)
                self.api_key = future.result()
            
            if not self.api_key:
                print("ERROR: {}: Can't register user. Maybe token has expired. Try getting new token.".format(self.name))
                return
        
        # save api_key to .ini
        global option_api_key
        option_api_key = self.api_key
        ini_write(fn_config, 'op', 'api_key', option_api_key)

        self.manager_dir = tempfile.mkdtemp(prefix=self.name+'_')
        self.executable = self.get_executable()
        
        if not os.path.exists(self.executable):
            self.download_server(self.executable)
        
        self.run_server(self.executable, self.manager_dir)
        
    def run_server(self, executable, manager_dir):
        if not IS_WIN:
            os.chmod(executable, 0o755)
        args = [
            executable,
            '--api_server_url', API_URL,
            '--manager_dir', manager_dir,
        ]
        startupinfo = None
        if IS_WIN:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.process = subprocess.Popen(args, startupinfo=startupinfo)
        
        def wait_for_port_file():
            self.port = None
            while self.port is None:
                self.find_port()
                time.sleep(0.3)
                
        with ThreadPoolExecutor() as ex:
            future = ex.submit(wait_for_port_file)
            while not future.done():
                app_idle()
                time.sleep(0.001)
        
    def find_port(self, tag=''):
        import re
        files = os.listdir(self.manager_dir)
        num_files = [f for f in files if re.match(r'^\d+$', f)]
        if num_files:
            self.port = int(num_files[0])
            pass;    LOG and print("Found port:", self.port)
            #print("Found port:", self.port)
            timer_proc(TIMER_START, self.heartbeat, 5000)
            msg_status("{}: Logged in".format(self.name))
            
    def get_completions(self):
        if self.port is None:
            self.log_in()
            
        with ThreadPoolExecutor() as ex:
            future = ex.submit(self.request_completions)
            while not future.done():
                app_idle()
                time.sleep(0.001)
            items = future.result()
            
            if items is None:
                return
            
            msg_status("{}: Got {} items".format(self.name, len(items)))
            
            ## debug
            #ed.cmd(cmds.cmd_FileNew)
            #ed.insert(0, 0, str(items))
            
            completions = []
            for comp in items:
                text = comp['completion']['text']
                text_inline = ''
                text_inline_mask = ''
                text_block = ''
                inline_num = 1
                parts = comp.get('completionParts', [])
                for part in parts:
                    if part['type'] == 'COMPLETION_PART_TYPE_INLINE':
                        if inline_num == 1:
                            text_inline = part['text']
                        else:
                            text_inline += part.get('prefix', '') + part['text']
                        inline_num += 1
                    elif part['type'] == 'COMPLETION_PART_TYPE_INLINE_MASK':
                        text_inline_mask = part['text']
                    elif part['type'] == 'COMPLETION_PART_TYPE_BLOCK':
                        text_block = part['text']
                        
                #if inline_num > 1:
                    #print("ERROR: inline_num=", inline_num)
                
                start_position = comp['range']['startPosition']
                end_position   = comp['range']['endPosition']
                start_position = (int(start_position.get('col', 0)), int(start_position.get('row', 0)))
                end_position   = (int(end_position.get('col', 0)), int(end_position.get('row', 0)))
                
                suffix = comp.get('suffix', None)
                cursor_offset = int(suffix.get('deltaCursorOffset', 0)) if suffix else 0
                suffix = suffix['text'] if suffix else ''
                
                def rep_chars(text):
                    return text.replace('\n',' ').replace('\t',' ')
                
                if text_inline:
                    hint = rep_chars(text_inline) + ' ' + rep_chars(text_block)
                else:
                    hint = rep_chars(text)
                
                completions.append(Item(
                    hint,
                    text,
                    suffix,
                    text_inline,
                    text_inline_mask,
                    text_block,
                    start_position,
                    end_position,
                    cursor_offset,
                ))
            
            self.completions = completions
            
            words = ['{}\t{}\t{}|{}'.format(
                        item.hint,
                        '',
                        '', i)
                        for i,item in enumerate(completions)
                    ]
            
            ed.complete_alt('\n'.join(words), SNIP_ID, len_chars=0)

    def on_snippet(self, ed_self: Editor, snippet_id, snippet_text):
        if snippet_id != SNIP_ID or '|' not in snippet_text:
            return
        _, item_ind = snippet_text.split('|')
        item_ind = int(item_ind)
        
        item = self.completions[item_ind]
        
        new_caret = ed_self.replace(
            item.start_position[0],
            item.start_position[1],
            item.end_position[0],
            item.end_position[1],
            item.text + item.suffix
        )
        
        if item.cursor_offset:
            offset = ed_self.convert(CONVERT_CARET_TO_OFFSET, new_caret[0], new_caret[1])
            offset += item.cursor_offset
            new_caret = ed_self.convert(CONVERT_OFFSET_TO_CARET, offset, 0)
            new_caret = (new_caret[0], new_caret[1], -1, -1)
        
        ed_self.set_caret(*new_caret)

        
    def heartbeat(self, *args):
        def _heartbeat_request():
            url = 'http://127.0.0.1:{}/exa.language_server_pb.LanguageServerService/Heartbeat'.format(
                self.port
            )
            
            data = {
                'metadata': {
                    'api_key': self.api_key,
                    'ide_name': 'vscode',
                    'ide_version': '1.77.3',
                    'extension_version': self.language_server_version,
                    }
            }
            
            try:
                response = requests.post(url, headers=HEADERS_JSON, data=json.dumps(data), timeout=4)
                response.raise_for_status()
            except requests.exceptions.Timeout:
                print("ERROR: Heartbeat failed: The request timed out.")
                return
            except requests.exceptions.RequestException as e:
                print("ERROR: Heartbeat failed. Error:", e)
                return
            
            result = response.content
            
            result_str = result.decode('utf-8')
            return result_str
        
        with ThreadPoolExecutor() as ex:
            future = ex.submit(_heartbeat_request)
            while not future.done():
                app_idle()
                time.sleep(0.001)
    
    def Ask(self):
        if self.port is None:
            self.log_in()
            
        if self.in_process_of_creating_new_tab:
            timer_proc(TIMER_START_ONE, lambda _: self.Ask(), 10)
        else:
            #question = dlg_input('Enter your question:', '')
            question = Dialog.input()
            
            if question is None:
                return
            self.request_GetChatMessage(question)
    
    def request_GetChatMessage(self, question):
        url = 'http://127.0.0.1:{}/exa.language_server_pb.LanguageServerService/GetChatMessage'.format(
            self.port
        )
        
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from . import proto_pb2
        
        GetChatMessage_data = proto_pb2.GetChatMessageRequest()
        GetChatMessage_data.prompt = question or ''
        
        metadata = proto_pb2.Metadata()
        metadata.api_key = self.api_key
        metadata.ide_name = "vscode"
        metadata.locale = "en"
        metadata.ide_version = "Visual Studio Code 1.77.3"
        metadata.extension_version = self.language_server_version
        metadata.extension_name = "vscode"
        metadata.session_id = SESSION_ID
        #metadata.session_id = "50d517c6-ac4a-4d44-ab20-1d48e12ee70d"
        GetChatMessage_data.metadata.CopyFrom(metadata)
        
        from .google.protobuf.timestamp_pb2 import Timestamp
        import datetime
        now = datetime.datetime.now()
        timestamp = Timestamp()
        timestamp.FromDatetime(now)
        
        chat_message = proto_pb2.ChatMessage()
        chat_message.messageId = 'user-1'
        chat_message.intent.generic.text = GetChatMessage_data.prompt
        chat_message.source = 1
        chat_message.timestamp.CopyFrom(timestamp)
        
        unique_string = str(uuid.uuid4())
        #chat_message.conversationId = unique_string
        chat_message.conversationId = '8HTVPeFtS35MLqygNelEYA8Ky8Qd32jG'
        self.messages.append(chat_message)
        GetChatMessage_data.chat_messages.extend(self.messages)
        
        
        
        #recordChat_data = proto_pb2.RecordChatPanelSessionRequest()
        #recordChat_data.metadata.CopyFrom(metadata)
        ##recordChat_data.startTimestamp.CopyFrom(timestamp)
        #recordChat_data.endTimestamp.CopyFrom(timestamp)
        #
        #data = recordChat_data.SerializeToString()
        #compression_flag = b'\x00'
        #data = compression_flag + len(data).to_bytes(4, 'big') + data
        #
        #try:
        #    response = requests.post(url, headers=HEADERS_CONNECT_PROTO, data=data, timeout=8)
        #    response.raise_for_status()
        #    print("response.content:", response.content)
        #    
        #except requests.exceptions.Timeout:
        #    print("ERROR: RecordChatPanelSession failed: The request timed out.")
        #    return
        #except requests.exceptions.RequestException as e:
        #    print("ERROR: RecordChatPanelSession failed. Error:", e)
        #    return
        
        
        
        data = GetChatMessage_data.SerializeToString()
        compression_flag = b'\x00'
        data = compression_flag + len(data).to_bytes(4, 'big') + data
        
        msg_status('{}: waiting for bot..'.format(self.name), process_messages=True)
        
        try:
            response = requests.post(url, headers=HEADERS_CONNECT_PROTO, data=data, timeout=8, stream=True)
            response.raise_for_status()
            messages = []
            partial_chunk = b''
            ed_handle = None
            
            for data in response.iter_content(chunk_size=8192):
                while data:
                    #print("data len", len(data))
                    #print("partial_chunk len", len(partial_chunk))
                    #if len(partial_chunk) > 0:
                        #raise
                    data = partial_chunk + data # prepend previous chunk if any
                    current_chunk = data # remember bytes, just in case
                    data = data[1:] # cut compressed flag from data
                    msg_length = int.from_bytes(data[0:4], 'big')
                    
                    if msg_length < 10: # small msg can't be parsed (this is final empty message)
                        break
                    
                    data = data[4:] # cut msg_length integer
                    
                    if msg_length > len(data)+2:
                        print("ERROR: msg_length is bigger then data len: ", msg_length, '>', len(data))
                        raise 
                    
                    msg = None
                    try:
                        msg = proto_pb2.GetChatMessageResponse().FromString(data[:msg_length])
                    except Exception as e:
                        #print("ERROR:", e, ':', data)
                        print("ERROR: can't decode chunk, let's save it to use with next chunk")
                        print("ERROR: current_chunk", current_chunk)
                        raise
                        partial_chunk = current_chunk
                        continue
                        
                    data = data[msg_length:] # cut parsed chunk
                    
                    if msg is None:
                        continue
                    partial_chunk = b''
                    
                    messages.append(msg)
                    
                    editor = self.get_editor(msg.chat_message.conversationId, question)
                    
                    from .google.protobuf.internal import encoder, decoder
                    
                    buf = messages[-1].chat_message.action.text
                    if buf:
                        # first byte is '\n' for some reason. some kind of mark?
                        buf = buf[1:] # skip it
                        # next we have varint? seems it's text size? what for? decode it and skip
                        varint, varint_len = decoder._DecodeVarint(buf, 0)
                        buf = buf[varint_len:]
                    
                    editor.set_text_all(buf.decode('utf-8', errors='replace'))
                    
                    
                    editor.cmd(cmds.cCommand_GotoTextEnd)
                    app_idle()
                        
            if not messages:
                print("{}: NOTE: no answer :(".format(self.name))
            else:
                msg_status('{}: answer recieved'.format(self.name), process_messages=True)
                editor.set_prop(PROP_CARET_VIEW, self.caret_view)
                
                self.messages.append(messages[-1].chat_message)
            return
            
        except requests.exceptions.Timeout:
            print("ERROR: GetChatMessage failed: The request timed out.")
            return
        except requests.exceptions.RequestException as e:
            print("ERROR: GetChatMessage failed. Error:", e)
            return
            
    def get_editor(self, conversation_id, question):
        ed_handle = self.conversations.get(conversation_id, None)
        if not ed_handle:
            self.in_process_of_creating_new_tab = True
            ed.cmd(cmds.cmd_FileNew)
            ed_handle = ed.get_prop(PROP_HANDLE_SELF)
            self.conversations[conversation_id] = ed_handle
            self.in_process_of_creating_new_tab = False

            tab_title = question.replace('\n', ' ')[:50]
            ed.set_prop(PROP_TAB_TITLE, 'Bot | {}'.format(tab_title))
            #ed.set_prop(PROP_LEXER_FILE, 'Markdown')
            ed.set_prop(PROP_LEXER_FILE, 'Log files ^')
            ed.set_prop(PROP_WRAP, WRAP_ON_WINDOW)
            self.caret_view = ed.get_prop(PROP_CARET_VIEW)
        ed.set_prop(PROP_CARET_VIEW, '-100,-100')
        return Editor(ed_handle)
    
    def request_completions(self, *args):
        if self.port is None:
            print("ERROR: Can't get completions: server is not started.")
            return
        
        url = 'http://127.0.0.1:{}/exa.language_server_pb.LanguageServerService/GetCompletions'.format(
            self.port
        )
        
        self.text = ed.get_text_all()
        self.col, self.row = ed.get_carets()[0][:2]
        
        lexer = ed.get_prop(PROP_LEXER_FILE)
        lang =  language_enum.get(lex_ids.get(lexer,''), 0)
        
        data = {
            'metadata': {
                'api_key': self.api_key,
                'ide_name': 'vscode',
                'ide_version': '1.77.3',
                'extension_version': self.language_server_version,
                },
            'document': {
                'text': self.text,
                'editor_language': lexer,
                'language': lang,
                'cursor_position': {
                    'row': self.row,
                    'col': self.col,
                },
                #'absolute_path': '',
                #'relative_path': '',
            },
            'editor_options': {
                'tab_size': ed.get_prop(PROP_TAB_SIZE),
                'insert_spaces': ed.get_prop(PROP_TAB_SPACES),
            },
            #'other_documents': {},
        }
        try:
            response = requests.post(url, headers=HEADERS_JSON, data=json.dumps(data), timeout=4)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            print("ERROR: Can't get completions: The request timed out")
            return
        except requests.exceptions.RequestException as e:
            print("ERROR: Can't get completions. Error:", e)
            return
        result = response.content
        
        result_str = result.decode('utf-8')
        result_json = json.loads(result_str)
        
        #print("get_completions:", result_json)
        
        #print("message: ", result_json['state'])
        items = result_json.get('completionItems', [])
        return items
        
    def shutdown(self, *args, **vargs):
        pass;       LOG and print('{}: shutting down'.format(self.name))
        
        self.port = None
        if self.process:
            if IS_WIN:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.call(['taskkill', '/F', '/T', '/PID',  str(self.process.pid)], startupinfo=startupinfo)
            else:
                self.process.terminate()
                self.process.wait()
            self.process = None


    def on_close(self, ed_self: Editor):
        '''
        Remove conversation from dict when the tab is closed.
        '''
        ed_h = ed_self.get_prop(PROP_HANDLE_SELF)
        to_pop = [k for k, v in self.conversations.items() if v == ed_h]
        
        for conversation_id in to_pop:
            self.conversations.pop(conversation_id, None)

    def on_exit(self, ed_self):
        self.shutdown()


language_enum = {
    'unspecified': 0,
    'c': 1,
    'clojure': 2,
    'coffeescript': 3,
    'cpp': 4,
    'csharp': 5,
    'css': 6,
    'cudacpp': 7,
    'dockerfile': 8,
    'go': 9,
    'groovy': 10,
    'handlebars': 11,
    'haskell': 12,
    'hcl': 13,
    'html': 14,
    'ini': 15,
    'java': 16,
    'javascript': 17,
    'json': 18,
    'julia': 19,
    'kotlin': 20,
    'latex': 21,
    'less': 22,
    'lua': 23,
    'makefile': 24,
    'markdown': 25,
    'objectivec': 26,
    'objectivecpp': 27,
    'perl': 28,
    'php': 29,
    'plaintext': 30,
    'protobuf': 31,
    'pbtxt': 32,
    'python': 33,
    'r': 34,
    'ruby': 35,
    'rust': 36,
    'sass': 37,
    'scala': 38,
    'scss': 39,
    'shell': 40,
    'sql': 41,
    'starlark': 42,
    'swift': 43,
    'typescriptreact': 44,
    'typescript': 45,
    'visualbasic': 46,
    'vue': 47,
    'xml': 48,
    'xsl': 49,
    'yaml': 50,
    'svelte': 51,
}

# taken from LSP plugin
lex_ids = {
    'ABAP': 'abap',
    'Batch files': 'bat', # spec: 'Windows Bat'
    'BibTeX': 'bibtex',
    'Clojure': 'clojure',
    'CoffeeScript': 'coffeescript', # spec: 'Coffeescript'
    'C': 'c',
    'C++': 'cpp',
    'C#': 'csharp',
    'CSS': 'css',
    'Diff': 'diff',
    'Dart': 'dart',
    'Dockerfile': 'dockerfile',
    'Elixir': 'elixir',
    'Erlang': 'erlang',
    'F#': 'fsharp',
    #'Git': 'git-commit and git-rebase', #TODO
    'Go': 'go',
    'Groovy': 'groovy',
    'HTML Handlebars': 'handlebars', # spec: 'Handlebars'
    'HTML': 'html',
    'Ini files': 'ini', # spec: 'Ini'
    'Java': 'java',
    'JavaScript': 'javascript',
    #'JavaScript React': 'javascriptreact', # Not in CudaText
    'JSON': 'json',
    'LaTeX': 'latex',
    'LESS': 'less', # spec: 'Less'
    'Lua': 'lua',
    'Makefile': 'makefile',
    'Markdown': 'markdown',
    'Objective-C': 'objective-c',
    #'Objective-C++': 'objective-cpp', # Not in CudaText
    'Perl': 'perl',
    #'Perl 6': 'perl6', # Not in CudaText
    'PHP': 'php',
    'PowerShell': 'powershell', # spec: 'Powershell'
    'Pug': 'jade',
    'Python': 'python',
    'R': 'r',
    'Razor': 'razor', # spec: 'Razor (cshtml)'
    'Ruby': 'ruby',
    'Rust': 'rust',
    #'SCSS': 'scss (syntax using curly brackets), sass (indented syntax)', #TODO
    'Scala': 'scala',
    #'ShaderLab': 'shaderlab', # not in CudaText
    'Bash script': 'shellscript', # spec: 'Shell Script (Bash)'
    'SQL': 'sql',
    'Swift': 'swift',
    'TypeScript': 'typescript',
    #'TypeScript React': 'typescriptreact', # Not in CudaText
    #'TeX': 'tex', # Not in CudaText
    #'Visual Basic': 'vb', # Not in CudaText
    'XML': 'xml',
    'XSLT': 'xsl', # spec: 'XSL'
    'YAML': 'yaml',
}

class Dialog:
    text = ''
    cancelled = True
    
    @classmethod
    def on_key_down(cls, id_dlg, id_ctl, data='', info=''):
        key, mod = data
        if mod == 'c' and key == keys.VK_ENTER:
            cls.cancelled = False
            cls.on_send(id_dlg, id_ctl)
            
    
    @classmethod
    def on_send(cls, id_dlg, id_ctl, data='', info=''):
        memo = Editor(dlg_proc(id_dlg, DLG_CTL_HANDLE, name='memo'))
        cls.text = memo.get_text_all()
        dlg_proc(id_dlg, DLG_HIDE)
        
        
    @classmethod
    def input(cls):
        h=dlg_proc(0, DLG_CREATE)
        dlg_proc(h, DLG_PROP_SET, prop={
            'cap': 'Codeium chat',
            'w': 500,
            'h': 400,
        })
        
        _, font_size = ed.get_prop(PROP_FONT)
        font_scale = ed.get_prop(PROP_SCALE_FONT)
        
        idc=dlg_proc(h, DLG_CTL_ADD, 'label');
        dlg_proc(h, DLG_CTL_PROP_SET, index=idc, prop={
            'cap': 'Enter your question below.\nCtrl+Enter to send.',
            'align': ALIGN_TOP,
            'sp_a': 2,
        })
        
        idc=dlg_proc(h, DLG_CTL_ADD, 'editor');
        dlg_proc(h, DLG_CTL_PROP_SET, index=idc, prop={
            #'border': DBORDER_NONE,
            'name': 'memo',
            'align': ALIGN_CLIENT,
            'font_size': font_size,
            #'sp_a': 2,
            'on_key_down': cls.on_key_down,
        })
        memo = Editor(dlg_proc(h, DLG_CTL_HANDLE, index=idc))
        memo.set_prop(PROP_SCALE_FONT, font_scale)
        if cls.cancelled:
            memo.set_text_all(cls.text)
        else:
            cls.text = ''
        cls.cancelled = True
        
        idc=dlg_proc(h, DLG_CTL_ADD, 'button');
        dlg_proc(h, DLG_CTL_PROP_SET, index=idc, prop={
           'name': 'btn_ok',
           'cap': 'Send',
           'align': ALIGN_BOTTOM,
           'sp_a': 6,
           'on_change': cls.on_send,
           'ex0': True,
        })
        
        dlg_proc(h, DLG_SCALE)
        
        timer_proc(TIMER_START_ONE, lambda _: memo.cmd(cmds.cCommand_GotoTextEnd), 10)
        dlg_proc(h, DLG_SHOW_MODAL)
        
        if cls.cancelled:
            cls.text = memo.get_text_all()
        
        dlg_proc(h, DLG_FREE)
        
        if not cls.cancelled:
            return(cls.text or None)
        
