import sys, time, paramiko
HOST, USER, PW = "10.81.0.205", "nobo", "nobohub"
with open(sys.argv[1], "rb") as f:
    body = f.read().replace(b"\r\n", b"\n")
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PW, timeout=30)
sftp = cli.open_sftp()
with sftp.file("/tmp/_run.sh", "wb") as f:
    f.write(body)
sftp.chmod("/tmp/_run.sh", 0o700)
sftp.close()
chan = cli.get_transport().open_session()
chan.get_pty()
chan.exec_command("cd /opt/nobo-control && bash /tmp/_run.sh 2>&1")
buf, sent = b"", False
while True:
    if chan.recv_ready():
        d = chan.recv(65536)
        if not d: break
        buf += d
        sys.stdout.write(d.decode("utf-8","replace")); sys.stdout.flush()
        if not sent and b"password for" in buf.lower():
            chan.send(PW + "\n"); sent = True
    elif chan.exit_status_ready():
        while chan.recv_ready():
            sys.stdout.write(chan.recv(65536).decode("utf-8","replace"))
        break
    else:
        time.sleep(0.2)
rc = chan.recv_exit_status()
print(f"\n=== exit {rc} ===")
cli.close(); sys.exit(rc)