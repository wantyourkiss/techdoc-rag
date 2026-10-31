"""Three-entry HTTPS release gate; credentials are read from protected environment."""
import argparse,json,os
from pathlib import Path
from techdoc.entrypoints import verify_entrypoints


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--lan-url',required=True);p.add_argument('--public-url',required=True)
    p.add_argument('--lan-ca',required=True);p.add_argument('--timeout',type=float,default=8);p.add_argument('--output')
    args=p.parse_args()
    try:
        report=verify_entrypoints(lan_url=args.lan_url,public_url=args.public_url,lan_ca=args.lan_ca,
            credentials=(os.getenv('TECHDOC_BROWSER_USER',''),os.getenv('TECHDOC_BROWSER_PASSWORD','')),timeout=args.timeout)
    except ValueError as exc:
        report={'passed':False,'error_code':str(exc),'model_calls':0}
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        # Operators should use a private output directory; never put credentials
        # in command arguments. The report itself contains only safe metadata.
        Path(args.output).write_text(text+'\n',encoding='utf-8')
    print(text)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
