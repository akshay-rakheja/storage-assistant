"""Fake MCP Apps host for end-to-end tests of the inline view.

Runs a real Plugin, serves one page that embeds the real bundled view in a sandboxed iframe and speaks the
MCP Apps postMessage protocol to it. Tests drive it from Playwright (see harness.js). Nothing here is product code.

    python3 tests/e2e/host.py [--port N]      prints "ready <port>"
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "storage-assistant"))
from storage_assistant import engine as engine_module, plugin as plugin_module  # noqa: E402
from storage_assistant.apps import UI_URI  # noqa: E402
from storage_assistant.executor import FakeExecutor  # noqa: E402
from storage_assistant.plugin import Plugin  # noqa: E402

UI = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
TMP = Path(os.environ.get("E2E_TMP") or tempfile.gettempdir()).resolve()
state = {"p": None, "n": 0, "made": [], "ui": True, "fake": None, "icloud": False, "opened": []}
REAL_POPEN = engine_module.subprocess.Popen


class ICloud:
    """Checks double: every folder counts as iCloud-synced (the real check needs iCloud Drive)."""
    def __init__(self, real):
        self.real = real

    def is_icloud_synced(self, path):
        return True

    def __getattr__(self, name):
        return getattr(self.real, name)


def fresh():
    if state["p"]:
        state["p"].close()
    p = Plugin(chooser=lambda: state.get("picked"), opener=lambda u: True)
    p.protocol.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": UI if state["ui"] else {}}})
    state["p"] = p
    p.cleanup.open_trash = lambda: state["opened"].append("trash") or {"opened": True}  # never open Finder in tests
    if state["fake"]:
        p.cleanup.executor = FakeExecutor(state["fake"])
    if state["icloud"]:
        p.cleanup.checks = ICloud(p.cleanup.checks)


def rpc(method, params):
    state["n"] += 1
    return state["p"].protocol.handle({"jsonrpc": "2.0", "id": state["n"], "method": method, "params": params})


def backdate(path, days):
    when = time.time() - days * 86400
    os.utime(path, (when, when), follow_symlinks=False)


def build(spec):
    """spec: [{p, size?, sparse?, dir?, link?, age?}] -> folder path. Ages (days) are applied last, deepest first."""
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="sa-e2e-", dir=TMP)) / spec.get("name", "fixture")
    base.mkdir()
    state["made"].append(base.parent)
    aged = []
    for entry in spec["entries"]:
        target = base / entry["p"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("dir"):
            target.mkdir(parents=True, exist_ok=True)
        elif "link" in entry:
            os.symlink(entry["link"], target)
        elif "hardlink" in entry:
            os.link(base / entry["hardlink"], target)
        else:
            with target.open("wb") as stream:
                if entry.get("sparse"):
                    stream.truncate(entry.get("size", 0))
                else:
                    left = entry.get("size", 0)
                    block = b"\0" * min(left, 1 << 20)
                    while left > 0:
                        stream.write(block[:left])
                        left -= len(block)
        if "age" in entry:
            aged.append((target, entry["age"]))
    default = spec.get("age")
    if default is not None:
        every = sorted((p for p in base.rglob("*")), key=lambda p: -len(p.parts))
        listed = {t for t, _ in aged}
        for path in every:
            if path not in listed:
                backdate(path, default)
    for target, days in sorted(aged, key=lambda item: -len(item[0].parts)):
        backdate(target, days)
    return str(base)


def slow_popen(seconds):
    def popen(cmd, **kwargs):
        return REAL_POPEN(["/bin/sh", "-c", 'sleep "$0"; exec "$@"', str(seconds)] + list(cmd), **kwargs)
    return popen


THROTTLE = "import sys,time\nfor l in sys.stdin:\n sys.stdout.write(l); sys.stdout.flush(); time.sleep(float(sys.argv[1]))"


def throttled_popen(per_line):
    """The real worker, with its output slowed down so a scan can be watched (and cancelled) part-way."""
    def popen(cmd, **kwargs):
        return REAL_POPEN(["/bin/sh", "-c", '"$@" | "$0" -u -c "$THROTTLE" "$PER_LINE"', sys.executable] + list(cmd),
                          **dict(kwargs, env=dict(kwargs.get("env") or {}, THROTTLE=THROTTLE, PER_LINE=str(per_line))))
    return popen


def control(body):
    op, p = body.get("op"), state["p"]
    if op == "fixture":
        return {"path": build(body["spec"])}
    if op == "restart":
        fresh()
    elif op == "ui":
        state["ui"] = bool(body["on"])
        fresh()
    elif op == "offer":
        p.updates.available = {"version": body.get("version", "9.9.9"), "url": body.get(
            "url", "https://github.com/akshay-rakheja/storage-assistant/releases/latest/download/storage-assistant.mcpb")}
    elif op == "expire_grant":
        with p.engine.lock:
            p.engine.grant.expires = 0
    elif op == "expire_cursors":
        with p.engine.lock:
            for key, (fingerprint, offset, _) in list(p.engine.cursors.items()):
                p.engine.cursors[key] = (fingerprint, offset, 0)
    elif op == "expire_tokens":
        with p.cleanup.lock:
            for record in p.cleanup.tokens.values():
                record["expiresAt"] = 0
    elif op == "limits":
        plugin_module.PLUGIN_LIMITS = dict(plugin_module.PLUGIN_LIMITS, **body["limits"])
        fresh()
    elif op == "opened":
        return {"opened": state["opened"]}
    elif op == "scan_wait":
        plugin_module.SCAN_WAIT = body["seconds"]
    elif op == "scan_delay":
        engine_module.subprocess.Popen = slow_popen(body["seconds"]) if body["seconds"] else REAL_POPEN
    elif op == "scan_throttle":
        engine_module.subprocess.Popen = throttled_popen(body["perLine"]) if body["perLine"] else REAL_POPEN
    elif op == "fake_executor":
        state["fake"] = tempfile.mkdtemp(prefix="sa-e2e-trash-", dir=TMP)
        state["made"].append(Path(state["fake"]))
        p.cleanup.executor = FakeExecutor(state["fake"])
        return {"trash": state["fake"]}
    elif op == "fail_executor":
        p.cleanup.executor.fail = {name: OSError(13, "denied") for name in body["names"]}
    elif op == "icloud":
        state["icloud"] = True
        p.cleanup.checks = ICloud(p.cleanup.checks)
    elif op == "touch":
        backdate(body["path"], body.get("age", 0))
    elif op == "write":
        with open(body["path"], "ab") as stream:
            stream.write(b"\0" * body.get("size", 1))
    elif op == "remove":
        target = Path(body["path"])
        if TMP not in target.resolve().parents:
            raise ValueError("outside the fixture area")
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    elif op == "exists":
        return {"exists": os.path.lexists(body["path"])}
    elif op == "listdir":
        return {"names": sorted(os.listdir(body["path"]))}
    elif op == "pick":
        state["picked"] = body.get("path")
    elif op == "state":
        with p.engine.lock:
            grant, scan = p.engine.grant, p.engine.scan
            return {"grant": grant.path if grant else None, "scan": scan and {
                "scanId": scan["scanId"], "scanVersion": scan["scanVersion"], "state": scan["state"],
                "nodes": len(scan["nodes"])}, "tokens": len(p.cleanup.tokens)}
    else:
        raise ValueError("unknown op")
    return {"ok": True}


PAGE = """<!doctype html><meta charset=utf-8><title>fake host</title>
<body style="margin:0;background:#fff"><iframe id=f sandbox="allow-scripts" style="width:WIDTHpx;height:1100px;border:0"></iframe><script>
const f=document.getElementById('f');
async function post(u,b){return (await fetch(u,{method:'POST',body:JSON.stringify(b||{})})).json();}
// mode: what this pretend host supports. rec: everything the view asked the host for.
window.mode={advertise:true,allow:true,context:true,theme:'light',delay:0,delayTool:null,hang:[],reject:[],links:true,vars:null};
window.rec={calls:[],links:[],sizes:[],context:[],notes:[],init:null,results:[]};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
window.addEventListener('message',async e=>{const m=e.data;if(e.source!==f.contentWindow||!m||m.jsonrpc!=='2.0')return;
 if(m.id===undefined){ if(m.method==='ui/notifications/size-changed'){rec.sizes.push(m.params.height);f.style.height=Math.max(200,m.params.height)+'px';} else rec.notes.push(m.method); return; }
 if(!m.method){rec.results.push(m);return;}
 let result={};
 const fail=(code,message)=>f.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,error:{code,message}},'*');
 if(m.method==='ui/initialize'){rec.init=m.params;const caps={};if(mode.context)caps.updateModelContext={structuredContent:{}};if(mode.advertise)caps.serverTools={};
   result={protocolVersion:'2026-01-26',hostCapabilities:caps,hostContext:Object.assign({theme:mode.theme},mode.vars?{styles:{variables:mode.vars}}:{})};}
 else if(m.method==='ui/open-link'){if(!mode.links)return fail(-32601,'no');rec.links.push(m.params.url);}
 else if(m.method==='ui/update-model-context'){rec.context.push(m.params);}
 else if(m.method==='tools/call'){const entry={name:m.params.name,args:m.params.arguments,at:Date.now()};rec.calls.push(entry);
   if(!mode.allow||mode.reject.includes(m.params.name))return fail(-32601,'no');
   if(mode.hang.includes(m.params.name))return;
   if(mode.delay&&(!mode.delayTool||mode.delayTool===m.params.name))await sleep(mode.delay);
   result=(await post('/rpc',{method:'tools/call',params:m.params})).result;entry.result=result;}
 else return fail(-32601,'Method not supported');
 f.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result},'*');});
window.load=async()=>{const r=await post('/rpc',{method:'resources/read',params:{uri:'URI'}});f.srcdoc=r.result.contents[0].text;await new Promise(x=>f.onload=x);};
window.send=(method,params,id)=>f.contentWindow.postMessage(Object.assign({jsonrpc:'2.0',method,params},id===undefined?{}:{id}),'*');
window.replay=r=>window.send('ui/notifications/tool-result',r);
window.call=async(name,args)=>(await post('/rpc',{method:'tools/call',params:{name,arguments:args}})).result;
window.rpc=(method,params)=>post('/rpc',{method,params});
window.ctl=(op,args)=>post('/ctl',Object.assign({op},args||{}));
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, kind="application/json"):
        self.send_response(200)
        self.send_header("content-type", kind)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        width = "760"
        if "?w=" in self.path:
            width = str(int(self.path.split("?w=")[1]))
        self.reply(PAGE.replace("WIDTH", width).replace("URI", UI_URI).encode(), "text/html")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
        try:
            out = control(body) if self.path == "/ctl" else rpc(body["method"], body.get("params", {}))
        except Exception as error:  # test harness: report, never die
            out = {"harnessError": f"{type(error).__name__}: {error}"}
        self.reply(json.dumps(out).encode())


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    fresh()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("ready", server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state["p"].close()
        for made in state["made"]:
            shutil.rmtree(made, ignore_errors=True)


if __name__ == "__main__":
    main()
