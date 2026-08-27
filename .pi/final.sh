set -e
cd /opt/nobo-control
sudo git fetch -q origin
sudo git checkout -q -B main origin/main
sudo git reset -q --hard origin/main
sudo git branch --set-upstream-to=origin/main main >/dev/null 2>&1 || true
echo "=== final state ==="
echo "  branch : $(git rev-parse --abbrev-ref HEAD)"
echo "  commit : $(git log --oneline -1)"
echo "  track  : $(git status -sb | head -1)"
sudo bash scripts/update.sh 2>&1 | tail -2
sleep 25
echo
echo "  containers:"
sudo docker ps --format '    {{.Names}}\t{{.Status}}' | grep -i nobo
echo "  service: $(systemctl is-enabled nobo-control) / $(systemctl is-active nobo-control)"
echo
CA=/opt/nobo-control/nobo-root.crt
echo "  https /api/health : $(curl -s --cacert $CA -o /dev/null -w '%{http_code}' --max-time 10 https://nobo.ababoy.com/api/health)"
echo "  https /favicon.ico: $(curl -s --cacert $CA -o /dev/null -w '%{http_code} %{content_type}' --max-time 10 https://nobo.ababoy.com/favicon.ico)"
echo "  http  -> redirect : $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://nobo.ababoy.com/)"
timeout 4 bash -c 'echo > /dev/tcp/10.81.0.205/8000' 2>/dev/null && echo "  port 8000: OPEN" || echo "  port 8000: closed (correct)"
echo
echo "  git working tree:"
git status --short | sed 's/^/    /' || true
echo "  (blank = clean)"
rm -f /tmp/_run.sh
