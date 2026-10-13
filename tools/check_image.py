"""Check HA's anonymous GHCR access; do not use local Docker credentials."""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def check(image):
    match=re.fullmatch(r'ghcr.io/([a-z0-9_./-]+):([a-zA-Z0-9_.-]+)',image)
    if not match: raise ValueError('Expected a tagged GHCR image')
    repository,tag=match.groups()
    query=urllib.parse.urlencode({'service':'ghcr.io','scope':f'repository:{repository}:pull'})
    with urllib.request.urlopen('https://ghcr.io/token?'+query,timeout=20) as response:
        token=json.load(response)['token']
    request=urllib.request.Request(f'https://ghcr.io/v2/{repository}/manifests/{tag}',headers={
        'Authorization':'Bearer '+token,'Accept':'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json'})
    with urllib.request.urlopen(request,timeout=20) as response:
        manifest=json.load(response)
        if manifest.get('schemaVersion')!=2: raise ValueError('Unexpected image manifest')
        digest=response.headers.get('Docker-Content-Digest')
    return digest


if __name__=='__main__':
    try: print('Anonymous image download verified:',check(sys.argv[1]))
    except (urllib.error.URLError,ValueError,KeyError) as error:
        # Never emit the bearer token or request headers.
        print('Anonymous image unavailable:',getattr(error,'code',type(error).__name__))
        sys.exit(1)
