"""Fixed, ephemeral model transport process. No state/action/scheduler imports."""
import json
import resource
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request,urlopen


def main():
    resource.setrlimit(resource.RLIMIT_CPU,(20,20))
    resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
    resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
    request=json.loads(sys.stdin.read(131073))
    data=request['payload']
    url=request['host']+'/api/generate'
    req=Request(url,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=request['timeout']) as response:
        raw=response.read(65537)
    if len(raw)>65536:
        raise ValueError('model response too large')
    result=json.loads(raw)
    # Provider thinking/debug fields never cross the transport boundary.
    out={'response':result['response'], 'usage':{key:result[key] for key in
         ('done','done_reason','eval_count','prompt_eval_count','total_duration','eval_duration','prompt_eval_duration','load_duration') if key in result}}
    sys.stdout.write(json.dumps(out))

if __name__=='__main__':
    try:
        main()
    except (TimeoutError, socket.timeout):
        sys.stderr.write('DREAM_ERROR:request_timeout')
        sys.exit(1)
    except HTTPError:
        sys.stderr.write('DREAM_ERROR:http_error')
        sys.exit(1)
    except json.JSONDecodeError:
        sys.stderr.write('DREAM_ERROR:provider_json')
        sys.exit(1)
    except (KeyError, TypeError):
        sys.stderr.write('DREAM_ERROR:provider_shape')
        sys.exit(1)
    except URLError as error:
        reason = getattr(error, 'reason', None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            sys.stderr.write('DREAM_ERROR:request_timeout')
        else:
            sys.stderr.write('DREAM_ERROR:transport_error')
        sys.exit(1)
    except Exception:
        # Never place provider payloads, URLs, or response content in diagnostics.
        sys.stderr.write('DREAM_ERROR:transport_error')
        sys.exit(1)
