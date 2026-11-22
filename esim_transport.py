import json, sys
from esim import Card

def reply(**values):
    print(json.dumps(values), flush=True)

try:
    with Card(sys.argv[1]) as card:
        reply(channel=card.channel)
        for line in sys.stdin:
            try:
                request=json.loads(line)
                reply(response=card.apdu(bytes.fromhex(request['apdu'])).hex())
            except Exception as exc:
                reply(error=str(exc))
except Exception as exc:
    reply(error=str(exc))
