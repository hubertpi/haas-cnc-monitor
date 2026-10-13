"""Bounded read-only capability report. Never enables a macro as actual feed."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'haas_monitor'))
from app.collector import MDCCollector, MDC_DIAGNOSTIC_VARIABLES, CollectionError
from app.models import Endpoint


async def inspect(endpoint):
    collector=MDCCollector()
    report={'endpoint':endpoint,'controller':{},'readings':{},'verified_for_oee':False,
            'notes':['No writes are sent. Readings alone do not establish active-feed semantics or units.',
                     'A zero axis macro does not prove that axis is installed.',
                     'Haas documents work-coordinate macros as not readable during motion.']}
    try:
        await collector.connect(endpoint)
        report['controller']=dict(collector.metadata)
        for variable,label in MDC_DIAGNOSTIC_VARIABLES.items():
            command='?Q600 '+str(variable)
            response=await collector.query(command,endpoint['timeout'])
            report['readings'][str(variable)]={'label':label,'query':command,'response':response,
                                              'units':'unverified','supported':response is not None}
    except (CollectionError,OSError) as error:
        report['error']=str(error)
    finally:
        await collector.close()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--port',type=int,default=5051,help='Existing Setting 143 / gateway port')
    parser.add_argument('--timeout',type=int,default=3)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    endpoint=Endpoint(host=args.host,port=args.port,timeout=args.timeout,protocol='mdc').model_dump()
    report=asyncio.run(inspect(endpoint))
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(args.output)
    if 'error' in report:sys.exit(1)
