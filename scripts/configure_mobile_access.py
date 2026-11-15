"""Prepare local LAN HTTPS. Does not install system trust or login startup items."""
import argparse
from datetime import datetime,timedelta,timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID

ROOT=Path(__file__).resolve().parent.parent
TLS=ROOT/'.runtime/mobile-tls'


def private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data)
    path.chmod(0o600)


def lan_address():
    for interface in ('en0','en1','en2'):
        try:
            result=subprocess.run(['/usr/sbin/ipconfig','getifaddr',interface],capture_output=True,text=True)
            value=result.stdout.strip()
            if value and ipaddress.ip_address(value).is_private and not ipaddress.ip_address(value).is_loopback:return value
        except (OSError,ValueError):pass
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
        probe.connect(('192.0.2.1',80))
        value=probe.getsockname()[0]
    if not ipaddress.ip_address(value).is_private or ipaddress.ip_address(value).is_loopback:
        raise ValueError('No private LAN address found; provide --host explicitly')
    return value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',help='Private LAN IPv4 address, defaults to the Wi-Fi interface')
    parser.add_argument('--https-port',type=int,default=8766)
    args=parser.parse_args()
    host=args.host or lan_address();ip=ipaddress.ip_address(host)
    if ip.version!=4 or not ip.is_private or ip.is_loopback:raise SystemExit('Use a private LAN IPv4 address')
    if not 1024<=args.https_port<=65535 or args.https_port in (8642,8765):raise SystemExit('Invalid HTTPS port')
    TLS.mkdir(parents=True,exist_ok=True);TLS.chmod(0o700)
    now=datetime.now(timezone.utc)
    ca_key_path=TLS/'root-ca.key';ca_cert_path=TLS/'root-ca.crt'
    if ca_key_path.exists() and ca_cert_path.exists():
        ca_key=serialization.load_pem_private_key(ca_key_path.read_bytes(),password=None)
        ca=x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    else:
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Personal Agent Local Demo CA')])
        ca=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
            .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True)
            .sign(ca_key,hashes.SHA256()))
        private(ca_key_path,ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        private(ca_cert_path,ca.public_bytes(serialization.Encoding.PEM))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Personal Agent Local Demo')])
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(ca.subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip),x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.DNSName('localhost')]),critical=False)
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
          .sign(ca_key,hashes.SHA256()))
    private(TLS/'server.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    private(TLS/'server.crt',cert.public_bytes(serialization.Encoding.PEM)+ca.public_bytes(serialization.Encoding.PEM))
    config={'host':host,'allowed_hosts':[host],'http_host':'0.0.0.0','https_host':'0.0.0.0','https_port':args.https_port,
            'mobile_url':f'https://{host}:{args.https_port}/mobile','trust_url':f'http://{host}:8765/mobile/connect',
            'certfile':str(TLS/'server.crt'),'keyfile':str(TLS/'server.key')}
    private(ROOT/'.runtime/mobile-access.json',(json.dumps(config,indent=2)+'\n').encode())
    print(json.dumps({'mobile_url':config['mobile_url'],'setup_url':config['trust_url'],'system_trust_installed':False,'auto_start_installed':False}))


if __name__=='__main__':main()
