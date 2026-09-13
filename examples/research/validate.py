"""Example contract check. Source accuracy remains the manager's responsibility."""
import json
import sys
from pathlib import Path

if sys.argv[1:] == ['--check']:
    sys.exit(0)
try:
    packet = json.loads(Path(sys.argv[1]).read_text())
    assert isinstance(packet, dict), 'packet must be an object'
    assert isinstance(packet.get('summary'), str) and packet['summary'].strip(), 'summary required'
    assert isinstance(packet.get('sources'), list) and packet['sources'], 'sources required'
    for source in packet['sources']:
        assert isinstance(source, dict), 'source must be an object'
        assert isinstance(source.get('url'), str) and source['url'].startswith(('https://', 'http://')), 'source URL required'
        assert isinstance(source.get('claim'), str) and source['claim'].strip(), 'source claim required'
    assert isinstance(packet.get('limitations'), list), 'limitations required'
    assert all(isinstance(x, str) for x in packet['limitations']), 'limitations must be strings'
except (ValueError, AssertionError, OSError) as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
