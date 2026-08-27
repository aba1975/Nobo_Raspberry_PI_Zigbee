set -e
cd /opt/nobo-control
sudo git fetch -q origin
sudo git reset -q --hard origin/fix-update-url
echo "at: $(git log --oneline -1)"
echo
echo "############ with TLS on (current .env) ############"
grep -E '^(NOBO_DOMAIN|NOBO_BIND)=' .env | sed 's/^/  /'
sudo bash scripts/update.sh 2>&1 | tail -2
sleep 20
echo
echo "############ and with TLS off (temporarily) ############"
sudo cp .env /tmp/env.keep
sudo sed -i 's/^NOBO_BIND=127.0.0.1/NOBO_BIND=0.0.0.0/' .env
sudo bash scripts/update.sh 2>&1 | tail -2
echo
echo "############ restore ############"
sudo cp /tmp/env.keep .env
sudo rm -f /tmp/env.keep
grep -E '^NOBO_BIND=' .env | sed 's/^/  restored: /'
sudo bash scripts/update.sh 2>&1 | tail -2
sleep 20
echo
echo "=== final check ==="
curl -s --cacert /opt/nobo-control/nobo-root.crt -o /dev/null -w '  https -> %{http_code}\n' --max-time 10 https://nobo.ababoy.com/api/health
timeout 4 bash -c 'echo > /dev/tcp/10.81.0.205/8000' 2>/dev/null && echo "  8000 OPEN" || echo "  8000 closed (correct)"
rm -f /tmp/_run.sh
