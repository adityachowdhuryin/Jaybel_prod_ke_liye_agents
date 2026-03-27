from googleapiclient.discovery import build

svc = build('logging', 'v2')
parent = 'projects/gls-training-486405'
flt = 'resource.type="audited_resource" AND protoPayload.serviceName="aiplatform.googleapis.com" AND protoPayload.status.message:"aiplatform.reasoningEngines.get"'
req = {
    'resourceNames': [parent],
    'filter': flt,
    'orderBy': 'timestamp desc',
    'pageSize': 20,
}
resp = svc.entries().list(body=req).execute()
entries = resp.get('entries', [])
print('entries', len(entries))
for e in entries:
    p = e.get('protoPayload', {})
    ai = p.get('authenticationInfo', {})
    print('principal', ai.get('principalEmail'))
    print('method', p.get('methodName'))
    print('status', p.get('status', {}).get('message'))
    print('---')
