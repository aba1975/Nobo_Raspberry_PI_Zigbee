set -e
cd /opt/nobo-control
echo "=== move to main ==="
sudo git fetch -q origin
sudo git checkout -q -B main origin/main
sudo git reset -q --hard origin/main
sudo git branch --set-upstream-to=origin/main main >/dev/null 2>&1 || true
echo "  now on: $(git rev-parse --abbrev-ref HEAD) @ $(git log --oneline -1)"
echo "  tracking: $(git status -sb | head -1)"

echo
echo "=== tidy the .env backups I left ==="
ls -1 .env.bak.* 2>/dev/null | sed 's/^/  removing /' || echo "  none"
sudo rm -f .env.bak.*

echo
echo "=== .env TLS settings intact? (gitignored, should survive) ==="
grep -E '^(NOBO_DOMAIN|NOBO_BIND|NOBO_PORT|COMPOSE_PROFILES)=' .env | sed 's/^/  /'

echo
echo "=== rebuild and restart on main ==="
sudo bash scripts/update.sh 2>&1 | tail -3
sleep 25

echo
echo "=== containers ==="
sudo docker ps --format '{{.Names}}\t{{.Status}}' | grep -i nobo | sed 's/^/  /'
echo
echo "=== HTTPS + icon, signed out ==="
CA=/opt/nobo-control/nobo-root.crt
for p in /api/health /favicon.ico /login; do
  printf "  %-16s %s\n" "$p" "$(curl -s --cacert $CA -o /dev/null -w '%{http_code}  %{content_type}' --max-time 10 https://nobo.ababoy.com$p)"
done
echo
echo "=== plain HTTP still closed? ==="
timeout 4 bash -c 'echo > /dev/tcp/10.81.0.205/8000' 2>/dev/null && echo "  8000 OPEN (unexpected)" || echo "  8000 closed (correct)"
echo
echo "=== service ==="
systemctl is-enabled nobo-control; systemctl is-active nobo-control
rm -f /tmp/_run.sh
